package results

import (
	"encoding/json"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

var siteOrder = []string{"deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"}
var questionOrder = []string{"solver_vendors", "decision_ai_vendors", "ev_cars_global"}

var siteNames = map[string]string{
	"deepseek": "DeepSeek",
	"doubao":   "豆包",
	"yiyan":    "文心一言",
	"yuanbao":  "腾讯元宝",
	"qianwen":  "通义千问",
	"kimi":     "Kimi",
}

type Store struct {
	ExportsDir    string
	QuestionsPath string
}

type BatchFile struct {
	BatchID   string           `json:"batch_id"`
	Timestamp string           `json:"timestamp"`
	Sites     []string         `json:"sites"`
	Questions []any            `json:"questions"`
	Summary   map[string]any   `json:"summary"`
	Results   []map[string]any `json:"results"`
	File      string           `json:"file,omitempty"`
	MTime     string           `json:"mtime,omitempty"`
}

func (s *Store) listFiles() []string {
	matches, _ := filepath.Glob(filepath.Join(s.ExportsDir, "cn-ai-*.json"))
	sort.Slice(matches, func(i, j int) bool {
		ii, _ := os.Stat(matches[i])
		jj, _ := os.Stat(matches[j])
		if ii == nil || jj == nil {
			return matches[i] > matches[j]
		}
		return ii.ModTime().After(jj.ModTime())
	})
	return matches
}

func (s *Store) loadFile(path string) (*BatchFile, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var b BatchFile
	if err := json.Unmarshal(raw, &b); err != nil {
		return nil, err
	}
	if b.BatchID == "" {
		b.BatchID = strings.TrimSuffix(filepath.Base(path), ".json")
	}
	st, _ := os.Stat(path)
	if b.Timestamp == "" && st != nil {
		b.Timestamp = st.ModTime().Format(time.RFC3339)
	}
	if b.Summary == nil {
		ok := 0
		cites := 0
		for _, r := range b.Results {
			if truthy(r["ok"]) {
				ok++
			}
			if arr, ok2 := r["citations"].([]any); ok2 && len(arr) > 0 {
				cites++
			}
		}
		b.Summary = map[string]any{
			"total":          len(b.Results),
			"ok":             ok,
			"failed":         len(b.Results) - ok,
			"with_citations": cites,
		}
	}
	b.File = filepath.Base(path)
	if st != nil {
		b.MTime = st.ModTime().Format(time.RFC3339)
	}
	return &b, nil
}

func (s *Store) Find(batchID string) (*BatchFile, error) {
	direct := filepath.Join(s.ExportsDir, batchID+".json")
	if _, err := os.Stat(direct); err == nil {
		return s.loadFile(direct)
	}
	for _, p := range s.listFiles() {
		b, err := s.loadFile(p)
		if err != nil {
			continue
		}
		if b.BatchID == batchID {
			return b, nil
		}
	}
	return nil, os.ErrNotExist
}

func (s *Store) ListBatches() []map[string]any {
	out := []map[string]any{}
	for _, p := range s.listFiles() {
		b, err := s.loadFile(p)
		if err != nil {
			out = append(out, map[string]any{"batch_id": filepath.Base(p), "file": filepath.Base(p), "error": err.Error()})
			continue
		}
		out = append(out, map[string]any{
			"batch_id":  b.BatchID,
			"file":      b.File,
			"timestamp": b.Timestamp,
			"sites":     b.Sites,
			"questions": b.Questions,
			"summary":   b.Summary,
			"mtime":     b.MTime,
		})
	}
	return out
}

func (s *Store) Latest() (*BatchFile, error) {
	files := s.listFiles()
	if len(files) == 0 {
		return nil, os.ErrNotExist
	}
	return s.loadFile(files[0])
}

func (s *Store) Result(batchID, siteID, questionID string) (map[string]any, error) {
	b, err := s.Find(batchID)
	if err != nil {
		return nil, err
	}
	for _, r := range b.Results {
		if str(r["site_id"]) == siteID && str(r["question_id"]) == questionID {
			if str(r["site_name"]) == "" {
				r["site_name"] = siteNames[siteID]
			}
			return r, nil
		}
	}
	return nil, os.ErrNotExist
}

func (s *Store) questionMeta() map[string]map[string]string {
	meta := map[string]map[string]string{}
	raw, err := os.ReadFile(s.QuestionsPath)
	if err != nil {
		return meta
	}
	var qdata struct {
		Questions []struct {
			ID     string `json:"id"`
			Prompt string `json:"prompt"`
			Label  string `json:"label"`
			Title  string `json:"title"`
		} `json:"questions"`
	}
	if json.Unmarshal(raw, &qdata) != nil {
		return meta
	}
	for _, q := range qdata.Questions {
		if q.ID == "" {
			continue
		}
		label := q.Label
		if label == "" {
			label = q.Title
		}
		if label == "" {
			label = q.ID
		}
		meta[q.ID] = map[string]string{
			"question_id": q.ID,
			"prompt":      q.Prompt,
			"label":       label,
		}
	}
	return meta
}

func (s *Store) Tree() map[string]any {
	qmeta := s.questionMeta()
	type batchNode struct {
		BatchID   string
		Timestamp string
		Channels  map[string]map[string]any
	}
	type qNode struct {
		QuestionID string
		Prompt     string
		Label      string
		Batches    map[string]*batchNode
	}
	tree := map[string]*qNode{}
	files := s.listFiles()
	for _, p := range files {
		b, err := s.loadFile(p)
		if err != nil {
			continue
		}
		for _, r := range b.Results {
			qid := str(r["question_id"])
			sid := str(r["site_id"])
			if qid == "" || sid == "" {
				continue
			}
			if tree[qid] == nil {
				m := qmeta[qid]
				prompt := str(r["prompt"])
				label := qid
				if m != nil {
					if m["prompt"] != "" {
						prompt = m["prompt"]
					}
					if m["label"] != "" {
						label = m["label"]
					}
				}
				if prompt == "" {
					prompt = qid
				}
				tree[qid] = &qNode{QuestionID: qid, Prompt: prompt, Label: label, Batches: map[string]*batchNode{}}
			}
			if tree[qid].Prompt == qid && str(r["prompt"]) != "" {
				tree[qid].Prompt = str(r["prompt"])
			}
			bn := tree[qid].Batches[b.BatchID]
			if bn == nil {
				bn = &batchNode{BatchID: b.BatchID, Timestamp: b.Timestamp, Channels: map[string]map[string]any{}}
				tree[qid].Batches[b.BatchID] = bn
			}
			brief := channelBrief(r)
			prev := bn.Channels[sid]
			if prev == nil || asInt(brief["answer_len"]) >= asInt(prev["answer_len"]) {
				bn.Channels[sid] = brief
			}
		}
	}
	qids := make([]string, 0, len(tree))
	for id := range tree {
		qids = append(qids, id)
	}
	qids = sortKnown(qids, questionOrder)
	questions := []map[string]any{}
	for _, qid := range qids {
		node := tree[qid]
		bids := make([]string, 0, len(node.Batches))
		for id := range node.Batches {
			bids = append(bids, id)
		}
		sort.Slice(bids, func(i, j int) bool {
			return node.Batches[bids[i]].Timestamp > node.Batches[bids[j]].Timestamp
		})
		batchList := []map[string]any{}
		for _, bid := range bids {
			bn := node.Batches[bid]
			sids := make([]string, 0, len(bn.Channels))
			for id := range bn.Channels {
				sids = append(sids, id)
			}
			sids = sortKnown(sids, siteOrder)
			channels := make([]map[string]any, 0, len(sids))
			okN, citeN := 0, 0
			for _, sid := range sids {
				ch := bn.Channels[sid]
				channels = append(channels, ch)
				if truthy(ch["ok"]) {
					okN++
				}
				if asInt(ch["citation_count"]) > 0 {
					citeN++
				}
			}
			batchList = append(batchList, map[string]any{
				"batch_id":      bid,
				"timestamp":     bn.Timestamp,
				"channel_count": len(channels),
				"ok_count":      okN,
				"cite_count":    citeN,
				"channels":      channels,
			})
		}
		questions = append(questions, map[string]any{
			"question_id": qid,
			"prompt":      node.Prompt,
			"label":       node.Label,
			"batch_count": len(batchList),
			"batches":     batchList,
		})
	}
	return map[string]any{
		"question_count":   len(questions),
		"batch_file_count": len(files),
		"questions":        questions,
	}
}

func channelBrief(r map[string]any) map[string]any {
	cites, _ := r["citations"].([]any)
	answer := str(r["answer"])
	if answer == "" {
		answer = str(r["error"])
	}
	preview := answer
	if len([]rune(preview)) > 240 {
		preview = string([]rune(preview)[:240])
	}
	sid := str(r["site_id"])
	name := str(r["site_name"])
	if name == "" {
		name = siteNames[sid]
	}
	return map[string]any{
		"site_id":          sid,
		"site_name":        name,
		"ok":               truthy(r["ok"]),
		"citation_count":   len(cites),
		"citation_domains": r["citation_domains"],
		"source":           r["source"],
		"url":              r["url"],
		"timestamp":        r["timestamp"],
		"run_dir":          r["run_dir"],
		"error":            r["error"],
		"answer_preview":   preview,
		"answer_len":       len(answer),
	}
}

func sortKnown(ids, order []string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, id := range order {
		for _, x := range ids {
			if x == id {
				out = append(out, x)
				seen[x] = true
			}
		}
	}
	rest := []string{}
	for _, x := range ids {
		if !seen[x] {
			rest = append(rest, x)
		}
	}
	sort.Strings(rest)
	return append(out, rest...)
}

func str(v any) string {
	if v == nil {
		return ""
	}
	switch t := v.(type) {
	case string:
		return t
	default:
		b, _ := json.Marshal(t)
		return strings.Trim(string(b), `"`)
	}
}

func truthy(v any) bool {
	switch t := v.(type) {
	case bool:
		return t
	case string:
		return t == "true" || t == "1"
	case float64:
		return t != 0
	default:
		return false
	}
}

func asInt(v any) int {
	switch t := v.(type) {
	case int:
		return t
	case float64:
		return int(t)
	default:
		return 0
	}
}

func HostOf(u string) string {
	p, err := url.Parse(u)
	if err != nil {
		return ""
	}
	return p.Hostname()
}
