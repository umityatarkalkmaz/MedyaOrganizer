# Media Organizer

Basit bir Python scripti: bir dizini tarayıp fotoğraf, video ve dokümanları
uzantılarına göre `OrganizedMedia` klasörüne kopyalar (isteğe bağlı taşır).

## Kullanım

```bash
python organize_media.py
```

İstenirse aşağıdaki seçenekler kullanılabilir:

```bash
python organize_media.py --source /yol/kaynak --output /yol/hedef --mode folder --move
```

| Bayrak | Açıklama |
|---|---|
| `--source` | Taranacak dizin (varsayılan: geçerli dizin) |
| `--output` | Çıktı dizini (varsayılan: `<source>/OrganizedMedia`) |
| `--mode` | `flat` (tek klasör) veya `folder` (kategoriye göre alt klasör) |
| `--move` | Kopyalamak yerine taşır (onay istenir) |

Bayrak verilmezse script gerekli bilgiyi interaktif olarak sorar.

## Desteklenen dosya türleri

- **Fotoğraf:** jpg, jpeg, png, gif, bmp, webp
- **Video:** mp4, mov, avi, mkv, webm
- **Doküman:** pdf, docx, xlsx, txt

## Gereksinimler

- Python 3.10+
