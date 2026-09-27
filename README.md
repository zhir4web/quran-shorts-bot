# Quran Shorts Bot

بۆتەکە ڤیدیۆی ستوونی 1080×1920 لە تلاوەت و پاشبنەما دروست دەکات و لە ڕێگەی GitHub Actions بۆ YouTube دەنێرێت. ئامانجی خشتە 06:00، 12:00 و 18:00 بە کاتی بەغدایە؛ ئەمانە کاتی ئامانجن، نە بەڵێنی بڵاوکردنەوە لە هەمان چرکەدا.

## دامەزراندن و تاقیکردنەوە

Python 3.12 پێویستە. لە Windows، `setup.bat` جێبەجێ بکە؛ یان:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/dashboard.test.js
```

Node.js 22 یان نوێتر بۆ تاقیکردنەوەکانی JavaScript پێویستە.

## ڕێکخستن

- `automation.json`: چالاکبوون، کەناڵ، privacy و `playlist_privacy` (بۆ playlist ـی خۆکاری سورەتەکان). ئەم کۆپییە `enabled: true` و `public` ـە؛ ئەمە دۆخی GitHub یان OAuth ناسەلمێنێت.
- `catalog.json`: قارییە ڕێپێدراوەکان، 30–58 چرکە تلاوەت و 1 چرکە کۆتایی بێدەنگ.
- `.github/workflows/daily.yml`: پۆستکردن و پشکنینی کارایی.
- `.github/workflows/lint.yml`: ڕەندەری ruff و ڤالیدەیشنی JSON ـی ڕێکخستن لەسەر هەر push و PR.
- `schedule_policy.py`: یاسای هاوبەشی خشتە بۆ بۆت و داشبۆرد.
- `.bot-state/published.json`: تۆماری پایەدار لە هەمان branch ـی workflow.

GitHub Actions پێویستی بە secret ـی `YOUTUBE_TOKEN` هەیە. `GITHUB_TOKEN` لە workflow دابین دەکرێت. scope ـەکان `youtube.upload`، `youtube.readonly`، `youtube.force-ssl` و `yt-analytics.readonly` ـن. بۆ login ـی ناوخۆیی، OAuth desktop client لە `client_secrets.json` دابنێ و `run.bat auth` جێبەجێ بکە. بۆ چالاککردنی داتای تەندروستی (retention)، تۆکین ـەکە بە scope ـی `yt-analytics.readonly` نوێ بکەرەوە؛ پێش ئەوە ڕاپۆرت وەک «نابەردەست» نیشان دەدرێت و بڵاوکردنەوە کاریگەری لێ نابینێت.

ئاگادارکردنەوەی GitHub بۆ workflow ـە شکست‌خواردووەکان لە ڕێکخستنی هەژماری خۆتەوە چالاک دەکرێت. `DISCORD_WEBHOOK_URL` بۆ ئاگادارکردنەوەی Discord ئارەزوومەندانەیە؛ `HEARTBEAT_URL` بۆ چاودێریی دەرەکییە. ئەگەر هیچ کامیان ڕێکنەخرابێت، تۆڕێکی دەرەکی پەیوەندی پێوە ناکرێت و بڵاوکردنەوە بەردەوام دەبێت.

لە Actions، `preview` بۆ پشکنینی ڤیدیۆ، `scheduled` بۆ گرتنەوەی slot، `publish` بۆ پۆستی دەستی، یان `report` بۆ نوێکردنەوەی دۆخی YouTube بەبێ بڵاوکردنەوە هەڵبژێرە. لە داشبۆردیش دوگمەی پشکنینی دۆخ هەیە؛ ئەنجام دوای تەواوبوونی workflow نوێ دەبێتەوە. `count` لە 1 تا 5 تەنها لە publish کار دەکات. پۆستەکانی یەک داواکاری لە یەک workflow بە ڕیز جێبەجێ دەبن؛ شکستی پۆستێک batch ڕادەگرێت.

پۆستی دەستیی دوای 06:00 لە ئامانجی خۆکاری ڕۆژەکەدا دەژمێردرێت. گرتنەوەی پۆستی دواخراو کەمترین 20 خولەک دوای پۆستی پێشووە. پۆستی دەستی دەتوانێت لە ئامانجی سێ پۆست زیاتر بێت.

## پاراستنی تلاوەت و ڤیدیۆ

- ئایەت نابڕدرێت؛ دەنگی زۆر کورت یان درێژ دەپەڕێندرێت. خێرایی و تۆنی دەنگ ناگۆڕدرێن.
- ماوە، دەنگ، قەبارە و جووڵە بە FFmpeg دەپشکنرێن.
- دەقی عوسمانی لە Quran Foundation وەردەگیرێت؛ ناسنامەی ئایەتی گەڕاوە ئەگەر هەبێت لەگەڵ داواکاری بەراورد دەکرێت.
- HarfBuzz و FreeType نووسین و حەرەکات دادەنێن. ئەگەر فۆنت پیتێکی نەبێت یان دەق جێ نەبێتەوە، ڕەندەر دەوەستێت.
- بۆ هەر یەک لە پێنج theme ـەکە کلیپێکی فیلمکراوی ڕاستەقینەی پاشبنەما هەیە؛ ئەگەر کلیپێک ون بێت، workflow بە هەڵە دەوەستێت و theme ـێکی تر بە بێ ئاگاداری جێگۆڕکێ ناکرێت. [سەرچاوەکانی کلیپ](assets/backgrounds/video/LICENSES.md).
- تۆماری uploading پێش ناردن دەپارێزرێت؛ video_id پێش کۆمێنت دەپارێزرێت. upload ـی نادیار پۆستکردن ڕادەگرێت تا پشکنین بکرێت.

Preview هیچ ڤیدیۆ یان کۆمێنتێک نانێرێت و cursor ـی دوور ناگۆڕێت. لە GitHub تۆمار دەخوێنێتەوە؛ لە ناوخۆ بەبێ GitHub credentials لە سەرەتای کاتەلۆگ دەست پێ دەکات.

## داشبۆرد

```powershell
$env:GITHUB_TOKEN = "token-with-actions-write-and-contents-read"
$env:DASHBOARD_KEY = "a-long-random-key"
$env:GITHUB_REPOSITORY = "owner/repository"
$env:GITHUB_BRANCH = "main"
.\.venv\Scripts\python.exe dashboard_server.py
```

`http://127.0.0.1:8787` بکەرەوە. لە دەرەوەی localhost، کلیل پێویستە و HTTPS بەکاربهێنە. `PORT` یان `DASHBOARD_PORT` پۆرت دیاری دەکات.

داشبۆرد تەنها پشکنینی سەرکەوتووی هەمان کەناڵ لە 24 کاتژمێری ڕابردوودا بە پشتڕاستکراو نیشان دەدات. بوونی ناسنامە بە مانای پەیوەستبوون نییە. upload ـی نادیار، شکستی workflow و ناچالاکبوونی بڵاوکردنەوە نیشان دەدرێن.

TikTok تەنها لاپەڕەی زانیارییە؛ پۆستکردنی هێشتا جێبەجێ نەکراوە.

ڕێنمایی زیاتر: [داشبۆرد](dashboard/README.md)، [خۆکارکردن](AUTOMATION.md)، [Cloud Run](CLOUD_RUN.md)، [پشکنین](AUDIT.md).


## چاودێری قورئانخوێنی نوێ

workflow ـی reciter watcher هەموو دووشەممە کاتژمێر 08:00 بە کاتی بەغداد لیستی Quran Foundation دەپشکنێت. پشکنینی یەکەم تەنها بنەمای داتا تۆمار دەکات؛ لە هەفتەکانی دواتردا ناسنامە نوێیەکان تەنها بۆ proposed_reciter_ids_for_review زیاد دەکرێن و هەرگیز خۆکارانە ناچنە allowlist. وردەکاری و ناوی قورئانخوێنە نوێیەکان لە Actions → Quran reciter watcher → Summary دەبینرێن؛ ئەگەر DISCORD_WEBHOOK_URL ڕێکخرابێت، ئاگادارکردنەوە لە Discord ـیش دەنێردرێت. شکستی API تەنها لە ڕاپۆرتی ئەم workflow ـەدا نیشان دەدرێت و کاریگەری لە پۆستکردنی ڕۆژانە نییە.

## باگراوندی تازەی Pexels

بۆ چالاککردنی کلیپی portraitی نوێی Pexels، API key ـێک لە Pexels Developer بگرە و لە
GitHub repository ـەکەدا بیخە ناو **Settings → Secrets and variables → Actions → New repository secret**.
ناوی secret دەبێت بە وردی `PEXELS_API_KEY` بێت. ئەم کلیلە تەنها لە GitHub Actions ـی
publish بەکاردێت؛ پێویست ناکات لە Vercel دایبنێیت.

بۆتەکە بە API کلیپی نوێی portrait بە کوالیتی گونجاو دەهێنێت. لە ١٠ پەڕەی یەکەمی ئەنجامی گەڕاندا کلیپی بەکارهاتوو دووبارە هەڵنابژێرێت؛ ئەگەر کلیپی نوێی گونجاو نەما، ڕەنگە دواتر دووبارە بەکاری بهێنێتەوە. ناوی دروستکەر و بەستەری
Pexels و license لە description زیاد دەکات. زانیاری ناسنامە و checksum لە ledger
دەمێنێت، خودی کلیپەکان دوای render پاک دەکرێنەوە. ئەگەر secret نەبێت یان API
بەردەست نەبێت، publish بە کلیپە ناوخۆییە پشکنراوەکان بەردەوام دەبێت.
