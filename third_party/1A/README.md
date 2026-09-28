# Personal Task Board

## Cài đặt

Tạo môi trường ảo:

```bash
python -m venv .venv
```

Kích hoạt môi trường:

```bash
.venv\Scripts\activate
```

Cài đặt thư viện:

```bash
pip install -r requirements.txt
```

Cài đặt trình duyệt Playwright:

```bash
playwright install
```

## Chạy chương trình

```bash
python main.py
```

## Sử dụng

1. Chương trình tự động mở trình duyệt.
2. Đăng nhập tài khoản Microsoft 365.
3. Hoàn thành MFA (nếu có).
4. Sau khi đăng nhập thành công, chương trình tự động thu thập dữ liệu.
5. Phiên đăng nhập được lưu lại để sử dụng cho các lần chạy sau.

## Chạy lại

```bash
.venv\Scripts\activate
python main.py
```