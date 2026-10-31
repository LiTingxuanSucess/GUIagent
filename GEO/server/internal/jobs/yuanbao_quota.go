package jobs

import "strings"

// Match the platform's entire refusal, rather than mentions inside an answer
// explaining quotas or a question quoted by the model.
func yuanbaoQuotaReply(site, answer string) bool {
	if site != "yuanbao" {
		return false
	}
	text := strings.TrimRight(strings.Join(strings.Fields(answer), ""), "。.")
	return text == "此模型的本月用量已用完，次月1日零点刷新，可切换至专家模式，使用Hy3模型继续对话"
}
