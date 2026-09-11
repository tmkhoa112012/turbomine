# TurboMines Console (bản web)

## Deploy lên Render
1. Đẩy thư mục này lên 1 repo GitHub (riêng tư, đừng để public).
2. Trên Render: **New → Web Service** → chọn repo.
   - Runtime: Python 3
   - Build command: `pip install -r requirements.txt`
   - Start command: để trống (đã có `Procfile`) hoặc dán:
     `gunicorn app:app --workers 2 --threads 4 --timeout 120`
3. Vào tab **Environment**, thêm biến `SECRET_KEY` = 1 chuỗi ngẫu nhiên dài
   (bắt buộc nên set, nếu không mỗi lần Render restart dyno thì session cũ
   sẽ mất hiệu lực vì key ký cookie đổi ngẫu nhiên).
4. Deploy xong, mở domain Render cấp → xác thực key → dán profile → chạy.

## Kiến trúc bảo vệ source code (đọc kỹ)
- **Logic cược (tạo round / mở ô / đoán thua / cashout / endpoint thật của
  turbogg4u) nằm 100% trong `app.py`, chạy trên server.** Trình duyệt không
  bao giờ tải file này về — chỉ nhận `templates/index.html` (giao diện) và
  JSON log qua `/api/logs`.
- Mọi endpoint `/api/*` đòi cookie session đã xác thực key hợp lệ
  (`@require_auth`). Ai gọi thẳng bằng `requests` mà không qua bước
  `/api/request-key` → nhập đúng key → `/api/verify-key` thì luôn nhận
  `401 unauthorized`, không lấy được gì.
- Có rate-limit thô (8 request/phút/IP) trên 2 endpoint xin key & xác thực
  key, hạn chế dò key bằng script.
- `<meta name="robots" content="noindex, nofollow">` + không có sitemap →
  không lên Google, nhưng KHÔNG chặn được người biết URL trực tiếp truy cập.

## GIỚI HẠN THẬT SỰ (đừng ảo tưởng là bất khả xâm phạm)
Không có cách nào chặn tuyệt đối việc ai đó dùng `requests`/`curl` để GET
trang HTML/JS công khai — về bản chất trình duyệt cũng chỉ là 1 HTTP
client, Render/mọi web server đều PHẢI trả nội dung cho bất kỳ ai gõ đúng
URL. Cái kiến trúc này bảo vệ được là:
1. **Thuật toán/logic cược không nằm trong phần gửi cho client** — dù ai
   đó tải hết HTML/JS về, họ chỉ thấy giao diện gọi `fetch("/api/start", …)`
   chứ không thấy bên trong `/api/start` làm gì.
2. **API không trả dữ liệu có nghĩa nếu chưa xác thực key** — script
   `requests` không có session hợp lệ thì chỉ nhận toàn `401`.
3. Muốn "bẻ" được, người ta buộc phải tự đi mua/xin key hợp lệ giống người
   dùng thật — tức là quay lại đúng cơ chế bán key bạn đang có, không phải
   lỗ hổng kỹ thuật.

Nếu cần thêm 1 lớp nữa: có thể ẩn domain thật sau Cloudflare + bật "Bot
Fight Mode"/Turnstile (chặn request không giống trình duyệt thật), nhưng
đây là biện pháp giảm phiền chứ không phải giải pháp tuyệt đối — nói để
tránh bạn kỳ vọng sai.

## Giới hạn kỹ thuật khác
- State (session xác thực + log của mỗi phiên chạy) lưu trong RAM của 1
  process — nếu Render restart dyno hoặc scale > 1 instance thì mất state.
  Đủ dùng cho 1 dyno free/starter; nếu cần nhiều instance thì phải chuyển
  `SESSIONS`/`ENGINES` sang Redis.
- Cấu trúc response thật của `/api/bets/place` khi mở ô vẫn chỉ đang đoán
  qua từ khoá (`LOSS_KEYWORDS`) như bản gốc `turbo.py` — nếu vẫn đoán sai,
  gửi lại đúng RAW response lúc dính bom để mình chỉnh chính xác.
