package sessions

import (
	"context"
	"fmt"
	"strings"
	"time"
)

// SendCodeOnExistingPage requires the held recovery lease and its exact
// target. It never opens/navigates a page or switches to another login flow.
func (m *Manager) SendCodeOnExistingPage(parent context.Context, site, targetID string) (*State, error) {
	if err := parent.Err(); err != nil {
		return nil, err
	}
	if targetID == "" || managerSiteTargetID(m, site) != targetID || !m.isBusy(site) {
		return nil, fmt.Errorf("原登录页面不可用")
	}
	tab, err := m.tab(site)
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(25 * time.Second)
	if d, ok := parent.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	ctx, cancel := context.WithDeadline(tab, deadline)
	stop := context.AfterFunc(parent, cancel)
	defer stop()
	defer cancel()
	text, err := pageText(ctx)
	if err != nil {
		return nil, err
	}
	if waitingSMS(text) || strings.Contains(text, "验证码已发送") {
		return m.set(site, "waiting_sms", "原页面验证码已发送，等待短信回调"), nil
	}
	if captchaOnPage(ctx, text) {
		return m.set(site, "need_captcha", "请在原页面完成验证码"), nil
	}
	hasForm, err := runJSBool(ctx, hasFormJS)
	if err != nil {
		return nil, err
	}
	if !hasForm {
		return m.set(site, "login_form", "请先在原页面打开手机号登录表单"), nil
	}
	m.mu.Lock()
	st := m.states[site]
	if st == nil {
		m.mu.Unlock()
		return nil, fmt.Errorf("未知站点")
	}
	if st.LastSendAt > 0 && nowUnix()-st.LastSendAt < 300 {
		copy := *st
		m.mu.Unlock()
		return &copy, nil
	}
	for otherSite, other := range m.states {
		if otherSite == site || other == nil {
			continue
		}
		switch other.Status {
		case "waiting_sms", "sending", "need_captcha", "submitting", "logging_in":
			copy := *st
			copy.Message = "同一账号正在登录其他平台，完成后再发码"
			m.mu.Unlock()
			return &copy, nil
		}
	}
	phone := m.Phone
	m.setLocked(site, "sending", "正在原页面发送验证码")
	m.mu.Unlock()
	if err := fillPhone(ctx, site, phone); err != nil {
		return m.set(site, "login_form", "原手机号表单暂不可用，未切换或重开页面"), err
	}
	agreeIfNeeded(ctx)
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	clicked := clickSendCode(ctx)
	if clicked || ctx.Err() != nil {
		// Cancellation after a click may hide the acknowledgement. Retain
		// cooldown so the next recovery cannot send the same SMS twice.
		m.mu.Lock()
		m.states[site].LastSendAt = nowUnix()
		m.states[site].LastSendTargetID = targetID
		m.mu.Unlock()
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if !clicked {
		return m.set(site, "login_form", "原页面暂未找到可用发码按钮"), nil
	}
	text, err = pageText(ctx)
	if err != nil {
		return nil, err
	}
	if captchaOnPage(ctx, text) {
		return m.set(site, "need_captcha", "已点击发送，请完成原页面验证码"), nil
	}
	if waitingSMS(text) || strings.Contains(text, "验证码已发送") {
		return m.set(site, "waiting_sms", "原页面验证码已发送，等待短信回调"), nil
	}
	return m.set(site, "login_form", "已提交发码请求，等待原页面返回结果"), nil
}
