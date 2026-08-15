# Media Organizer

Bir dizini özyinelemeli tarayıp dosyaları türlerine göre `OrganizedMedia` klasörüne
kopyalar (isteğe bağlı taşır). İçerik bazlı (SHA-256) yineleme tespiti yapar ve
kaynak ağacı **güvenilmeyen girdi** olarak ele alır.

## Kullanım

```bash
python organizer.py
```

İstenirse aşağıdaki seçenekler kullanılabilir:

```bash
python organizer.py --source /yol/kaynak --output /yol/hedef --mode folder --move
```

| Bayrak | Açıklama |
|---|---|
| `--source` | Taranacak dizin (varsayılan: geçerli dizin) |
| `--output` | Çıktı dizini (varsayılan: `<source>/OrganizedMedia`) |
| `--mode` | `flat` (tek klasör) veya `folder` (kategoriye göre alt klasör) |
| `--move` | Kopyalamak yerine taşır (onay istenir) |
| `--skip-unmatched` | Tanınmayan uzantıları `other` klasörüne koymak yerine tamamen atlar |

Bayrak verilmezse script gerekli bilgiyi interaktif olarak sorar.

`--output` ile `--source` aynı dizin olamaz; bu durumda script hata verip çıkar
(aksi hâlde her dosya "mevcut çıktı" sayılacağı için hiçbir şey işlenmezdi).

## Desteklenen dosya türleri

| Kategori | Uzantılar |
|---|---|
| `photo` | jpg, jpeg, png, gif, bmp, webp, svg, heic, heif, tiff, tif, ico |
| `video` | mp4, mov, avi, mkv, webm, flv, wmv, m4v, mpg, mpeg |
| `document` | pdf, docx, doc, xlsx, xls, pptx, ppt, txt, md, rtf, odt, csv |
| `audio` | mp3, wav, flac, aac, ogg, m4a, wma |
| `archive` | zip, rar, 7z, tar, gz, bz2, xz, tgz |
| `code` | py, js, ts, jsx, tsx, html, css, json, go, rs, c, cpp, h, hpp, java, sh, yaml, yml, sql, php, rb, swift, kt |
| `design` | psd, ai, sketch, fig, xd, aseprite, ase, blend, indd |
| `font` | ttf, otf, woff, woff2 |
| `ebook` | epub, mobi, azw3 |
| `installer` | exe, msi, dmg, pkg, deb, appimage, apk |
| `other` | yukarıdakilerin dışındaki her şey (`--skip-unmatched` ile atlanabilir) |

## Yineleme tespiti

Aynı içeriğe sahip dosyalardan yalnızca ilki kopyalanır. Atlananlar çıktı dizinindeki
`duplicates_report.txt` dosyasına yazılır; her çalıştırma zaman damgalı bir `# run ...`
başlığıyla dosyanın **sonuna eklenir**, önceki kayıtların üzerine yazılmaz.

Performans için karşılaştırma önce dosya boyutuna bakar: boyutu benzersiz olan bir
dosya hiç okunmaz ve hash'lenmez, çünkü farklı boyuttaki iki dosya aynı olamaz.

## Güvenlik davranışı

Kaynak dizin paylaşımlı bir klasör, indirilenler klasörü ya da açılmış bir arşiv
olabileceği için script şunları uygular:

- Sembolik bağlantılar hiçbir zaman takip edilmez (ne dosya ne dizin olarak).
- Yalnızca normal dosyalar işlenir; fifo/aygıt düğümleri atlanır.
- Hedef dosya adları `O_CREAT|O_EXCL|O_NOFOLLOW` ile atomik olarak ayrılır, böylece
  hedefe yerleştirilmiş bir sembolik bağlantı yazmayı çıktı dizini dışına yönlendiremez.
- setuid/setgid/sticky bitleri kopyaya taşınmaz; izinlerin yalnızca alt 9 biti korunur.
- Dosya adlarındaki kontrol karakterleri log ve rapor çıktısında kaçışlanır.

## Gereksinimler

- Python 3.10+ (yalnızca standart kütüphane)
