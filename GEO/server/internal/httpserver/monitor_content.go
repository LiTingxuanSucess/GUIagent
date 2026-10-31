package httpserver

import (
	"encoding/json"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

// Separate detail DTOs prevent content fields from ever being decoded from the
// metadata index or accidentally returned by the paginated list endpoint.
type monitorCitation struct {
	URL      string `json:"url"`
	Title    string `json:"title"`
	SiteName string `json:"site_name"`
}

type monitorResultContent struct {
	monitorResult
	Answer          string            `json:"answer"`
	ShareURL        string            `json:"share_url"`
	CitationDetails []monitorCitation `json:"citation_details"`
}

type monitorRequestContent struct {
	monitorRequest
	Prompt           string                 `json:"prompt"`
	RequestID        string                 `json:"request_id"`
	ContentAvailable bool                   `json:"content_available"`
	Results          []monitorResultContent `json:"results"`
}

type storedMonitorJob struct {
	ID        string                `json:"job_id"`
	Prompt    string                `json:"prompt"`
	RequestID string                `json:"request_id"`
	Results   []storedMonitorResult `json:"results"`
}

type storedMonitorResult struct {
	SiteID          string          `json:"site_id"`
	Site            string          `json:"site"`
	Prompt          string          `json:"prompt"`
	Answer          string          `json:"answer"`
	AnswerPlain     string          `json:"answer_plain"`
	AnswerRaw       string          `json:"answer_raw"`
	URL             string          `json:"url"`
	ShareURL        string          `json:"share_url"`
	Citations       json.RawMessage `json:"citations"`
	CitationDetails json.RawMessage `json:"citation_details"`
}

func (s *Server) monitorRequestContent(row monitorRequest) monitorRequestContent {
	detail := monitorRequestContent{monitorRequest: row, Results: make([]monitorResultContent, 0, len(row.Results))}
	for _, result := range row.Results {
		detail.Results = append(detail.Results, monitorResultContent{monitorResult: result, CitationDetails: []monitorCitation{}})
	}
	if !monitorIdentifier.MatchString(row.ID) || strings.Contains(row.ID, ":") {
		return detail
	}
	root := filepath.Join(s.cfg.Root, "data")
	if configured := strings.TrimSpace(os.Getenv("GEO_DATA_DIR")); configured != "" {
		root = configured
	}
	// The route validates the ID and requires a matching metadata row before
	// reaching this reader. Refuse symlinks and oversized/non-regular files.
	fd, err := unix.Open(filepath.Join(root, "jobs", row.ID+".json"), unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	if err != nil {
		return detail
	}
	f := os.NewFile(uintptr(fd), row.ID+".json")
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > 16<<20 {
		return detail
	}
	var job storedMonitorJob
	if json.NewDecoder(io.LimitReader(f, 16<<20)).Decode(&job) != nil || job.ID != "" && job.ID != row.ID {
		return detail
	}
	detail.ContentAvailable = job.Prompt != "" || job.RequestID != "" || len(job.Results) > 0
	detail.Prompt, detail.RequestID = job.Prompt, job.RequestID
	for _, result := range job.Results {
		if detail.Prompt == "" && result.Prompt != "" {
			detail.Prompt = result.Prompt
		}
		site := result.SiteID
		if site == "" {
			site = result.Site
		}
		if site == "yiyan" {
			site = "wenxinyiyan"
		}
		for i := range detail.Results {
			metadataSite := detail.Results[i].Site
			if metadataSite == "yiyan" {
				metadataSite = "wenxinyiyan"
			}
			if site != metadataSite {
				continue
			}
			answer := result.AnswerPlain
			if answer == "" {
				answer = result.Answer
			}
			if answer == "" {
				answer = result.AnswerRaw
			}
			detail.Results[i].Answer = answer
			detail.Results[i].ShareURL = monitorPublicURL(result.ShareURL)
			if detail.Results[i].ShareURL == "" {
				detail.Results[i].ShareURL = monitorPublicURL(result.URL)
			}
			detail.Results[i].CitationDetails = monitorContentCitations(result.CitationDetails, result.Citations)
			break
		}
	}
	return detail
}

func monitorContentCitations(batches ...json.RawMessage) []monitorCitation {
	refs := []monitorCitation{}
	seen := map[string]int{}
	for _, batch := range batches {
		var entries []json.RawMessage
		if json.Unmarshal(batch, &entries) != nil {
			continue
		}
		for _, entry := range entries {
			var ref monitorCitation
			if json.Unmarshal(entry, &ref.URL) != nil {
				// Old webpage results use string URLs; newer API results and
				// citation_details supply an object with optional metadata.
				if json.Unmarshal(entry, &ref) != nil {
					continue
				}
			}
			ref.URL = monitorPublicURL(ref.URL)
			if ref.URL == "" {
				continue
			}
			if index, exists := seen[ref.URL]; exists {
				if refs[index].Title == "" {
					refs[index].Title = ref.Title
				}
				if refs[index].SiteName == "" {
					refs[index].SiteName = ref.SiteName
				}
				continue
			}
			seen[ref.URL] = len(refs)
			refs = append(refs, ref)
		}
	}
	return refs
}

func monitorPublicURL(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.User != nil || u.Host == "" || u.Opaque != "" {
		return ""
	}
	u.Scheme = strings.ToLower(u.Scheme)
	if u.Scheme != "http" && u.Scheme != "https" {
		return ""
	}
	host := strings.ToLower(strings.TrimSuffix(u.Hostname(), "."))
	if ip := net.ParseIP(host); ip != nil {
		if !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
			return ""
		}
	} else {
		// Reject local names and browser-supported numeric IP shorthands
		// without performing a network/DNS request while viewing a detail.
		last := host[strings.LastIndex(host, ".")+1:]
		if !strings.Contains(host, ".") || strings.ContainsAny(host, "%\\") || strings.Trim(last, "0123456789") == "" {
			return ""
		}
		for _, suffix := range []string{".localhost", ".local", ".internal", ".lan", ".home"} {
			if strings.HasSuffix(host, suffix) {
				return ""
			}
		}
	}
	if port := u.Port(); port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 1 || n > 65535 {
			return ""
		}
		switch n {
		case 5900, 5901, 6080, 9222, 9223, 9229:
			return ""
		}
	}
	path := strings.ToLower(u.Path)
	if path == "/json" || strings.HasPrefix(path, "/json/") || path == "/devtools" || strings.HasPrefix(path, "/devtools/") {
		return ""
	}
	return u.String()
}
