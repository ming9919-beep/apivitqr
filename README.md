# apivitqr

VietQR Reader API service and browser based QR reader.

## Public web page

After GitHub Pages finishes its deployment, open <https://ming9919-beep.github.io/apivitqr/>. The static page reads QR images in the browser and parses the VietQR payload. It does not send the QR image to this server.

GitHub Pages hosts static files only. The FastAPI service below still needs a Python host (or can be run locally); Pages does not run the FastAPI API endpoints.

一个读取越南 VietQR 的 HTTP 服务，可接收二维码图片或二维码原始字符串，返回解析后的完整 TLV 字段、银行 BIN/账户、银行名称与 logo、金额以及 CRC 校验状态。

## 启动

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000/docs` 查看交互式 API 文档。

## API

- `GET /health`：健康检查
- `POST /api/v1/parse`：JSON `{ "payload": "..." }`
- `POST /api/v1/parse-image`：上传 `image` 文件字段，支持常见二维码图片

银行资料从 VietQR 公共银行目录 `https://api.vietqr.io/v2/banks` 查询；目录暂时不可用时，原始 BIN 和二维码字段仍会正常返回，银行名称/logo 为 `null`。可通过 `VIETQR_BANKS_URL` 环境变量替换目录地址。目录结果在进程内缓存，重启服务后刷新。

## 字段说明

- 转账二维码通常会携带银行 BIN 和收款账号，但未必包含收款人姓名。只有二维码内存在 EMV 字段 `59` 时才返回 `recipient_name`；服务不会通过账号向银行查询或猜测姓名。
- 标准 VietQR 的 `63` 字段是 CRC-16/CCITT-FALSE 校验值，不是加密密钥或认证凭证。服务会返回 CRC 值和有效性；QR 内容本身不提供可供服务提取的“密钥”。
- 完整原始载荷和所有解析字段会作为 API 响应返回。请在调用方使用 HTTPS，并避免把响应、账号或原始载荷写入公开日志。
