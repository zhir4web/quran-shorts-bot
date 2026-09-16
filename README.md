# Quran Shorts Bot — وەشانی چاککراوە

**دۆخی ئێستا:** بڵاوکردنەوەی خۆکار لە GitHub Actions چالاکە. ڤیدیۆکان بە شێوەی public، سێ جار ڕۆژانە لە 09:00، 15:00 و 21:00 بە کاتی بەغدا بڵاودەبنەوە. یەکەم upload ـی ڕاستەقینە لە 2026-09-16 سەرکەوتوو بوو. وردەکاری لە [AUTOMATION.md](AUTOMATION.md) ـە.

ئەم وەشانە لە دەنگی مۆڵەتپێدراو و وێنە ڤیدیۆی ستوونی دروست دەکات، یان ڤیدیۆی مۆڵەتپێدراو ئامادە دەکات، و بە YouTube API باری دەکات. خێرایی و تۆنی قاری ناگۆڕێت. ڤیدیۆکە 1080×1920 دەبێت و بە مەبەست سنووری ئەم وەشانە 60 چرکەیە.

## دەستپێکردن لە Windows

1. Python 3.11 یان نوێتر دابمەزرێنە؛ هەڵبژاردەی Add Python to PATH چالاک بکە.
2. فایلەکان لە ZIP دەربهێنە. `setup.bat` بکەرەوە و چاوەڕێی تەواوبوونی دامەزراندن بکە.
3. دەنگی قارییەک کە مۆڵەتی بەکارهێنانت هەیە بخەرە `assets/recitation.mp3`.
4. ناوەڕۆکی `queue.example.json` کۆپی بکە بۆ `queue.json`. ناوی قاری، سوورەت و ئایەتەکان، مۆڵەت و سەرچاوەکان بە دروستی پڕ بکەرەوە. دوای دڵنیابوون لە مۆڵەتی هەموو پێکهاتەکان `rights_confirmed` بکە `true`.
5. `start` کاتی دەستپێکردنە لە فایلە سەرچاوەکە بە چرکە، `duration` ماوەی بەشەکەیە. سەرەتا و کۆتایی بەشەکە لە سنووری ئایەتی تەواودا هەڵبژێرە؛ بۆتەکە مانای قورئان ناپشکنێت.
6. `run.bat` بکەرەوە. ئەمە تەنیا پێشبینین دروست دەکات. ئەنجام لە `state/renders/recitation-001/video.mp4` دەبێت.
7. ڤیدیۆکە ببینە و بە وردی گوێی لێ بگرە؛ ناوی سوورەت، ئایەت و قاری بپشکنە.

فایلی `queue.json` بە مەبەست بەتاڵە؛ هیچ دەنگێکی قورئان یان مۆڵەتی قاری لە ZIP ـەکەدا نییە. `assets/background.png` پاشبنەمایەکی دروستکراوی ئەم پڕۆژەیە و دەتوانیت بەکاری بهێنیت یان بە وێنەی مۆڵەتپێدراوی خۆت بیگۆڕیت.

## پەیوەستکردن بە یوتوب

1. لە Google Cloud پڕۆژەیەک دروست بکە و YouTube Data API v3 چالاک بکە.
2. OAuth consent ڕێک بخە؛ ئەگەر لە Testing دایە هەژمارەکەت وەک test user زیاد بکە.
3. OAuth client لە جۆری Desktop app دروست بکە. JSON ـەکە بە ناوی `client_secrets.json` لە تەنیشت `bot.py` دابنێ.
4. لە ناو فۆڵدەرەکە فرمانی `run.bat auth` جێبەجێ بکە. لە وێبگەڕ هەژمار و کەناڵی دروست هەڵبژێرە و مۆڵەت بدە.
5. `run.bat run --privacy private` یەک ڤیدیۆی چاوەڕوانکراو بە شێوەی تایبەت بار دەکات. لە YouTube Studio ئەنجامەکە بپشکنە.
6. بۆ ڤیدیۆ نوێیەکانی دواتر دەتوانیت `run.bat run --privacy public` بەکاربهێنیت.

ڤیدیۆیەک کە پێشتر بار کراوە دووبارە بار ناکرێت. بۆتە هەورییەکە تۆماری ڤیدیۆ بڵاوکراوەکان لە GitHub دەپارێزێت.

## کارکردنی خۆکار

دوای تاقیکردنەوە و پەیوەستکردنی کەناڵ، لە PowerShell لە ناو فۆڵدەرەکە:

```powershell
./install-schedule.ps1 -Time '12:00' -Privacy public
```

ئەمە لە Windows Task Scheduler ئەرکێکی ڕۆژانە بە ناوی `QuranShortsBot` دروست دەکات. هەر جار یەک دانەی داهاتووی ڕیزەکە بار دەکات. کاتەکە بە کاتی کۆمپیوتەرەکەتە. کۆمپیوتەر دەبێت کار بکات، ئینتەرنێتی هەبێت و تۆش چووبیتە ژوورەوە. ئەگەر PowerShell ڕێگە بە script نادات، دەتوانیت هەمان ئەرک لە Task Scheduler بە دەستی دروست بکەیت: program = `.venv/Scripts/python.exe` بە ڕێڕەوی تەواو، arguments = ڕێڕەوی تەواوی `bot.py` لە نێوان کوتیشن و `run --privacy public`.

وەشانی چالاک لە GitHub Actions کار دەکات؛ کۆمپیوتەرەکەت دەتوانێت کوژاوە بێت. Windows Task Scheduler تەنها هەڵبژاردەیەکی لوکاڵە و بۆ وەشانی هەوری پێویست نییە.

## زیادکردنی ناوەڕۆک

- بۆ هەر بەشێکی نوێ `id` ـێکی جیاواز دابنێ. هەمان بەش بە ناسنامەی نوێ مەخەرە ڕیزەکە؛ ڕێگریی دووبارەبوونەوە بە `id` ـە.
- `mode: compose` دەنگ و وێنە تێکەڵ دەکات. `source` دەبێت ڕێڕەوی دەنگ یان HTTPS ـی سەرچاوەی مۆڵەتپێدراو بێت.
- `mode: video` ڤیدیۆی مۆڵەتپێدراو ئامادە دەکات؛ نووسینەکانی لایەنەکان ناپەڕێنێت، بەڵکو بۆشایی لە دەوروبەر زیاد دەکات.
- بۆ دابەزاندنی خۆکار، URL ـی ڕاستەوخۆ یان URL ـێکی پشتگیریکراوی yt-dlp لە `source` دابنێ. سەرچاوەی پێویست بە login یان cookies لەم وەشانەدا ڕێک نەخراوە. گەڕانی هەڕەمەکی بۆ ڤیدیۆی خەڵکی نییە.
- مۆڵەتەکان دەبێت دەنگی قاری، پاشبنەما و هەر پێکهاتەیەکی تر بگرنەوە. `rights_confirmed` تۆماری پشتڕاستکردنەوەی تۆیە؛ پشکنینی یاسایی خۆکار نییە.
- بۆ دەقی ئایەت، وێنەیەکی پشکنراو لە `background` بەکاربهێنە. ژێرنووسی ئایەت بە کات‌بەندی یان دروستکردنی دەنگی AI لەم وەشانەدا نییە.

## تۆمار و چارەسەری کێشەکان

```text
run.bat doctor
run.bat status
run.bat preview
run.bat run --privacy private
```

تۆمارەکان لە `state/bot.log` و مێژووی کارەکان لە `state/jobs.sqlite3` ـن. فۆڵدەری `state` مەسڕەوە؛ پاراستنی ئەوە پێویستە بۆ ڕێگری لە بارکردنی دووبارە. تۆکەنی تایبەتی تێدایە، بۆیە بە ئاشکرا بڵاوی مەکەرەوە.

ئەگەر بارکردن کۆتایی نادیار بوو، بۆت دەوەستێت. سەرەتا لە YouTube Studio بپشکنە:

```text
run.bat resolve --id recitation-001 --video-id YOUTUBE_VIDEO_ID
```

ئەگەر بە دڵنیایی بار نەکراوە:

```text
run.bat resolve --id recitation-001 --confirmed-not-uploaded
```

پاشان دەتوانیت دووبارە `run` بکەیت. ئەگەر item ـێکی پێشوو دەستکاری بکەیت، بۆت ڕێگە نادات هەمان ناسنامە بە ناوەڕۆکی جیاواز بەکاربهێنرێت. کۆپییە پێشووکە بگەڕێنەوە، یان تەنیا بۆ ناوەڕۆکی نوێ ناسنامەی نوێ بەکاربهێنە. پێشبینینی پارێزراو هەمان ئەو ڤیدیۆیەیە کە بار دەکرێت؛ گۆڕینی دەنگ لە شوێنی خۆیدا پێشبینین نوێ ناکاتەوە.

## سنوور و پێداویستییە دەرەکییەکان

پڕۆژە OAuth ـە نەپشکنراوەکان لەوانەیە تەنیا بتوانن private بار بکەن؛ گۆڕینی کۆد ئەم سنوورە لانا‌بات. ڕەزامەندی OAuth، quota، بەردەستی سەرچاوەکان و پەیوەندی ئینتەرنێت دەتوانن کارەکە بوەستێنن. هەژماری OAuth لە Testing لەوانەیە پێویستی بە پەیوەستکردنەوە هەبێت. هیچ گەرەنتییەک بۆ نەهاتنی copyright claim یان قازانجکردن نییە.

- [YouTube upload API](https://developers.google.com/youtube/v3/docs/videos/insert)
- [OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- [YouTube monetization policies](https://support.google.com/youtube/answer/1311392)

## Verification / development

Run `python -m unittest discover -s tests -v` in an environment with `requirements.txt` installed. Tests cover real FFmpeg composition/conversion, queue validation, interrupted uploads, duplicate prevention, preview isolation, output corruption and lock behavior. OAuth and live YouTube publishing need your own credentials and are not integration-tested against your channel.

The active GitHub workflow uses a durable repository ledger to prevent duplicate uploads. It publishes one Short per run at three scheduled Baghdad times. The local Windows queue remains available for offline previews and manual operation.

