# GEO 提问接口错误码

错误码已并入 [API.md](./API.md) 第 5 节，请以该节为准。

第 5 节包含：

- HTTP 状态，以及 `400` / `401` 的 `detail`
- 任务级错误码：`platform_auth_unavailable`、`all_sites_failed`、`service_interrupted`
- 平台级错误码：`platform_auth_unavailable`、`platform_session_unavailable`、`platform_busy`、`platform_risk_control`、`platform_quota`、`sms_timeout`、`site_timeout`、`site_request_failed`
- 调用方处理顺序

覆盖 `POST /api/v1/ask`、`GET /api/v1/jobs`、`GET /api/v1/jobs/{job_id}` 和任务终态回调。短信回调见 `API.md` 第 7 节，不使用这组错误码。
