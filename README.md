# HyperOS CN Language Overlays

Hệ thống tự động biên dịch (compile), căn chỉnh (`zipalign`) và ký (`apksigner`) các **Runtime Resource Overlay (RRO)** ngôn ngữ cho ROM HyperOS CN thành các file `.apk` riêng lẻ.

## Cấu trúc thư mục

```text
.
├── .github/workflows/build-overlays.yml   # GitHub Actions CI/CD
├── overlays/                              # Thư mục chứa nguồn XML theo từng app
│   └── com.android.settings/
│       ├── overlay.json                   # Cấu hình RRO (tùy chọn)
│       └── res/
│           └── values-vi/
│               └── strings.xml
├── scripts/build_overlays.py              # Script build local & CI
└── config.json                            # Cấu hình SDK & mặc định toàn cục
```

## Thêm overlay mới

1. Tạo thư mục `overlays/<target_package>/res/values-<lang>/` (ví dụ: `overlays/com.miui.home/res/values-vi/strings.xml`).
2. *(Tùy chọn)* Tạo `overlays/<target_package>/overlay.json` nếu cần tùy chỉnh thuộc tính RRO:
   ```json
   {
     "targetPackage": "com.miui.home",
     "targetName": "MiuiHome",
     "priority": 100,
     "isStatic": true,
     "versionCode": 1,
     "versionName": "1.0"
   }
   ```
   Nếu cần kiểm soát hoàn toàn manifest, bạn có thể đặt file `AndroidManifest.xml` trực tiếp trong `overlays/<target_package>/`.

## Build trên máy cá nhân (Local)

### Yêu cầu
- Python 3.10+
- Java JDK (có `keytool`)
- Android SDK Build-Tools (`aapt2`, `zipalign`, `apksigner`) và `platforms/android-35/android.jar` (thiết lập qua `ANDROID_HOME` hoặc `ANDROID_SDK_ROOT`).

### Chạy build
```bash
# Kiểm tra cú pháp XML và sinh AndroidManifest.xml (không cần Android SDK)
python3 scripts/build_overlays.py --validate-only

# Build toàn bộ overlays ra thư mục dist/ (mặc định build tất cả ngôn ngữ có trong overlays/)
python3 scripts/build_overlays.py --out dist

# Chỉ build Tiếng Việt (vi) hoặc tất cả ngôn ngữ (all)
python3 scripts/build_overlays.py --lang-mode vi --out dist
python3 scripts/build_overlays.py --lang-mode all --out dist

# Đồng bộ XML từ Xiaomi.eu (Tiếng Việt hoặc tất cả ngôn ngữ)
python3 scripts/sync_xml.py --lang Vietnamese
python3 scripts/sync_xml.py --lang all

# Build riêng một số overlay
python3 scripts/build_overlays.py --only com.android.settings,com.miui.home --out dist
```

Nếu không truyền keystore riêng, script tự tạo `build/debug.keystore` để ký APK. Để dùng keystore riêng:
```bash
python3 scripts/build_overlays.py --keystore /path/to/my.keystore --keystore-pass ... --key-alias ... --key-pass ...
```

## GitHub Actions & Release

- **Push / PR vào `main`**: tự động build (`vi`) và verify tất cả `.apk`, xuất ra mục **Artifacts** (`hyperos-cn-overlays-vi`).
- **Run workflow (`workflow_dispatch`)**: chọn `language_mode` là `vi` (chỉ Tiếng Việt) hoặc `all` (tự động sync & build toàn bộ ngôn ngữ từ Xiaomi.eu).
- **Push tag `v*`** (ví dụ `v1.0.0`) hoặc chạy **Run workflow** với `create_release: true`: tự động tạo GitHub Release đính kèm toàn bộ file `*.apk`.
- Để ký bằng key riêng trên CI, thêm các GitHub Repository Secrets sau:
  - `KEYSTORE_BASE64`: nội dung file `.keystore` đã mã hóa base64
  - `KEYSTORE_PASS`: mật khẩu keystore
  - `KEY_ALIAS`: alias của key
  - `KEY_PASS`: mật khẩu của key
