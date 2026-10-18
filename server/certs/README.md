# certs/

放对公网生效的 TLS 证书（文件名与 `config.py` 默认一致）：

```
hh.ei.ac.cn.pem    证书链（fullchain：站点证书 + 中间证书）
hh.ei.ac.cn.key    私钥（PKCS8，chmod 600）
```

* 缺失时 `server.py` 会打 WARN 并降级为 **http** 启动（`/healthz` 里 `tls:false`）。
* 临时自签（仅联调，App 侧证书校验会失败）：`python server.py --self-signed`。
* 换路径：`HH_TLS_CERT` / `HH_TLS_KEY`。
* 若前置 nginx 终结 TLS，则本服务用 `--no-tls` 监听内网端口即可（此时 `config.PUBLIC_BASE`
  仍写死 `https://hh.ei.ac.cn:51120`，用于生成 APK 的 `download_url`）。

`*.pem` / `*.key` / `*.crt` 已在 `server/.gitignore`，不要提交。
