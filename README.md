# HyperOS CN Language Overlays

Hệ thống tự động biên dịch (compile), căn chỉnh (`zipalign`) và ký (`apksigner`) các **Runtime Resource Overlay (RRO)** ngôn ngữ cho ROM HyperOS CN thành các file `.apk` riêng lẻ.

## Cấu trúc thư mục

```text
.
├── .github/workflows/
│   ├── build-overlays.yml             # Build & release APK overlay
│   ├── extract-rom-strings.yml        # Tải ROM, unpack lấy string gốc + lọc phần chưa dịch
│   └── translate.yml                  # Dịch phần chưa dịch qua API
├── untranslated/<version>/            # 1. Ngôn ngữ gốc trích từ ROM
├── pending/<version>/                 # 2. Phần chưa dịch (hoặc gốc có update)
├── overlays/<version>/                # 3. Phần đã dịch (nguồn build APK)
├── scripts/
│   ├── build_overlays.py              # Script build local & CI
│   ├── extract_rom_strings.py         # Script unpack ROM & tách pending
│   ├── translate_strings.py           # Script dịch API có checkpoint
│   ├── string_state.py                # Logic so sánh 3 trạng thái XML
│   └── sync_xml.py                    # Script sync XML từ Xiaomi.eu
└── config.json                        # Cấu hình SDK theo từng phiên bản
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

# Build toàn bộ các phiên bản có overlay ra dist/<version>/
python3 scripts/build_overlays.py --out dist

# Build riêng một phiên bản (ví dụ os4-a17, os3-a15, miui14...)
python3 scripts/build_overlays.py --version os4-a17 --lang-mode vi --out dist
python3 scripts/build_overlays.py --version os4-a17 --lang-mode all --out dist

# Đồng bộ XML từ Xiaomi.eu vào phiên bản chỉ định
python3 scripts/sync_xml.py --version os4-a17 --lang Vietnamese
python3 scripts/sync_xml.py --version os4-a17 --lang all

# Build riêng một số overlay
python3 scripts/build_overlays.py --only com.android.settings,com.miui.home --out dist
```

Nếu không truyền keystore riêng, script tự tạo `build/debug.keystore` để ký APK. Để dùng keystore riêng:
```bash
python3 scripts/build_overlays.py --keystore /path/to/my.keystore --keystore-pass ... --key-alias ... --key-pass ...
```

## GitHub Actions & Release

- **Push / PR vào `main`**: tự động build (`vi`) và verify tất cả `.apk`, xuất ra mục **Artifacts** (`hyperos-cn-overlays-vi`).
- **Run workflow (`build-overlays.yml`)**:
  - Chọn **`string_source`**: `repo` (`overlays/`), `xiaomi-eu` (sync trực tiếp từ `Xiaomi.eu-HyperOS-4.0-XML-Global.git`), `xiaomi-eu+repo` (ưu tiên Xiaomi.eu, bù phần thiếu từ `overlays/`), `repo+xiaomi-eu`, hoặc `custom` (`sources/custom/`).
  - Chọn **`version`** (`all` hoặc từng phiên bản `os4-a17`..`miui14`) và **`language_mode`** (`vi` hoặc `all`).
- **Extract Untranslated Strings from ROM** (`.github/workflows/extract-rom-strings.yml`): nhập `rom_url` (link tải ROM `.zip` hoặc `.tgz`) và chọn `version` (`os4-a17`..`miui14`) để tự động tải ROM, unpack phân vùng, lưu string gốc vào `untranslated/<version>/` và lọc các chuỗi chưa dịch / có cập nhật vào `pending/<version>/`.
- **Translate Pending Strings** (`.github/workflows/translate.yml`): dịch các chuỗi trong `pending/<version>/` qua `translate.hzzmonet.io.vn` (chọn `model`, mặc định `cx/gpt-6.1-sol`). Cần thêm Secret `TRANSLATE_API_TOKEN` trong GitHub Repo Settings. Nếu API lỗi giữa chừng, workflow vẫn tự commit cả phần đã dịch (`overlays/`) lẫn phần chưa dịch còn lại (`pending/`).
- **Push vào `main` / tag `v*`** hoặc chạy **Run workflow** (`create_release` mặc định `true`): tự động tạo GitHub Release đính kèm file `.zip` theo từng phiên bản (và các file `*.apk` trực tiếp nếu build 1 phiên bản).
- Để ký bằng key riêng trên CI, thêm các GitHub Repository Secrets sau:
  - `KEYSTORE_BASE64`: nội dung file `.keystore` đã mã hóa base64
  - `KEYSTORE_PASS`: mật khẩu keystore
  - `KEY_ALIAS`: alias của key
  - `KEY_PASS`: mật khẩu của key
