# دۆخی بڵاوکردنەوەی خۆکار

ڕێکخستنی ئەم کۆپییە چالاکە و privacy ـەکە public ـە. ناسنامەی کەناڵ لە automation.json ـە؛ ئەمە ناسەلمێنێت کە workflow لە GitHub چالاکە یان OAuth هێشتا کار دەکات.

## خشتە

ئامانجەکان 06:00، 12:00 و 18:00 بە کاتی بەغدان. schedule_policy.py یاسای هاوبەشی بۆت و داشبۆردە. heartbeat ـەکان لە .github/workflows/daily.yml ـن. دواخستنی GitHub، داگرتن، ڕەندەر و YouTube دەتوانن کاتی ڕاستەقینە بگۆڕن. slot ـەکانی ئەمڕۆ بە ڕیز دەگیرێنەوە، بە کەمترین 20 خولەک بۆشایی دوای پۆستی پێشوو.

publish --count N تا پێنج پۆست لە یەک کاردا بە ڕیز جێبەجێ دەکات. شکستی پۆستێک batch ڕادەگرێت؛ پۆستە سەرکەوتووەکانی پێشوو لە تۆمار دەمێننەوە. داواکاریی دەستی لە سێ پۆستی ڕۆژانە زیاتر ڕێگەپێدراوە.

## تۆمار و گەڕاندنەوە

.bot-state/published.json لە هەمان branch ـی workflow نوێ دەکرێتەوە. uploading پێش ناردن دەپارێزرێت؛ uploaded و video_id پێش کۆمێنت دەپارێزرێن.

ئەگەر uploading ماوەتەوە، یەکەم YouTube Studio بپشکنە. ئەگەر ڤیدیۆ هەیە، تۆماری هەمان کار بە status: uploaded، video_id و uploaded_at ـی ڕاستەقینە تەواو بکە؛ hash، cursor و slot مەگۆڕە. ئەگەر بە دڵنیایی ڤیدیۆ نەنێردراوە، تەنها reservation ـی هەمان کار دوای backup لاببە. هەرگیز هەموو تۆمار مەسڕەوە.

بۆ ڕیزی ناوخۆیی SQLite، bot.py resolve هەیە؛ ئەو فەرمانە تۆماری GitHub ناگۆڕێت. Preview تۆماری دوور ناگۆڕێت و upload، comment یان ڕاپۆرتی گۆڕینی state جێبەجێ ناکات.

## دەق و پاشبنەما

دەنگ و دەقی عوسمانی لە Quran Foundation وەردەگیرێن. ئایەت نابڕدرێت و خێرا ناکرێت. سنوورەکە 30–58 چرکە تلاوەت و 1 چرکە کۆتایی بێدەنگە. گەڕان سنووردارە؛ ئەگەر کاندید نەدۆزرایەوە cursor بۆ گەڕانی دواتر دەپارێزرێت.

باگراوندی فیلمکراو چەند کلیپی هەیە: هەر theme ـێک دەتوانێت کلیپی سەرەکی (`theme.mp4`) و کلیپی زیادە (`theme_2.mp4` …) هەبێت، هەموویان لە LICENSES.md تۆمار کراون. تلاوەتەکە بە یەکسانی لەسەر کلیپەکان دابەش دەکرێت و بە concat لێکدەدرێن، بۆیە ڤیدیۆ چەند دیمەنێکی جیاواز پیشان دەدات لە جیاتی دووبارەبوونەوەی یەک کلیپ. کلیپی بێ ڕیزی مۆڵەت هەرگیز بەکار ناهێنرێت و کلیپی ون fail-closed ـە.

کلیپی نوێ لە داشبۆردەوە دەنێردرێت (بەشی YouTube → ناردنی کلیپی باگراوند): لینکی ڕاستەوخۆی MP4 + theme + ناونیشان. تۆمارەکە وەک pending لە `.bot-state/clip-submissions/` هەڵدەگیرێت و هەرگیز خۆکاری بڵاو نابووەتەوە؛ پێویستە مرۆیی مۆڵەت و کوالیتی پشتڕاست بکات.

حەرەکات بە HarfBuzz و FreeType دادەنرێن. پیتی نەناسراو یان دەقی دەرەوەی سنووری کارت ڕەندەر ڕادەگرێت. بۆ هەر یەک لە پێنج theme ـەکە کلیپی فیلمکراوی پاشبنەمای ڕاستەقینە هەیە؛ سەرچاوەکانیان لە assets/backgrounds/video/LICENSES.md ـە. ناوی کلیپی بەکارهاتوو لە تۆمار دەپارێزرێت و کلیپی theme ـێکی تر بە بێ ئاگاداری جێگۆڕکێ ناکرێت.

## چاودێری

پشکنینی کەناڵی سەرکەوتوو لە 24 کاتژمێری ڕابردوو نیشان دەدرێت، نە پەیوەندیی زیندووی بەردەوام. شکستی پشکنین دۆخەکە نادیار دەکات. ڤیدیۆی نەدۆزراوە، visibility ـی جیاواز، upload ـی نادیار و شکستی دوایین workflow نیشان دەدرێن. بۆ هەر ڤیدیۆ دوا 30 snapshot ـی metrics دەپارێزرێت.

هەر ڤیدیۆیەکی نوێ خۆکاری زیاد دەکرێت بۆ playlist ـی سورەتەکەی. ناونیشانی playlist دوو زمانەیە (سورة X | Y — Quran Shorts)، یەکەم جار دروست دەکرێت و ناسنامەکەی لە تۆمار کاش دەکرێت؛ بوونی پێشوو بە ناونیشان دەدۆزرێتەوە بۆ ئەوەی دووبارە دروست نەکرێت. `playlist_privacy` لە automation.json بڕیار دەدات: public، unlisted یان private. هەڵەی playlist هیچ کاتێک تۆماری upload ناگوڕێت و لە ڕەندی زیاتری خۆکار بەدوورە. پینکردنی کۆمێنت و پۆستی کۆمەڵگە لە Data API ـی فەرمی YouTube دا نەنراون و بەبێ پەسەندی فەرمی جێبەجێ نەکراون.

داتای retention لە YouTube Analytics API وەردەگیرێت بۆ ڤیدیۆکانی 30 ڕۆژی ڕابردوو (زۆرترین 30 ڤیدیۆ بۆ هەر ڕاپۆرتێک): بینین، کاتی چاودێری، و average view percentage وەک snapshot ـی دواتر لەسەر هەر کارێک پارێزراون. ئەم داتایە جۆرەکانی کۆمێنتی CTA ـیش بەراورد دەکات (`cta_performance` لە تۆمار)؛ بەراوردەکە پەیوەندییە نەک سەلماندن — بڕیار بە چەند ڤیدیۆیەکی بەرچاو بدە پێش گۆڕانکاری. بۆ چالاککردنی، یەک جار `run.bat auth` بە scope ـی نوێ جێبەجێ بکە یان تۆکینی `YOUTUBE_TOKEN` بە `yt-analytics.readonly` نوێ بکەرەوە؛ تا ئەو کاتە ڕاپۆرتەکە بە ڕوونی دەڵێت نابەردەستە. impressions و CTR ـی تامبنايل لە Analytics API ـی ڕاستەوخۆدا نییە و پێویستی بە Reporting API ـی بەشکەر هەیە.

ئاگادارکردنەوەکان بۆ Discord دەنێردرێن کاتێک `DISCORD_WEBHOOK_URL` ڕێکخرابێت: سەرکەوتنی upload، شکستی workflow، بلۆکردنی قاری بۆ مەترسی پاراستن، و فلاگی تەندروستی وەک video unavailable. ئاگادارکردنەوەی GitHub بۆ workflow ـە شکست‌خواردووەکانیش لە ڕێکخستنی هەژماری خۆتەوە چالاک دەکرێت. ناردنی ئاگادارکردنەوە هیچ کاریگەرییەکی لەسەر بڵاوکردنەوە نییە.

`HEARTBEAT_URL` لە کۆتایی هەر کارێک دەکوێژرێت و لە شکستدا بە `/fail`؛ خزمەتگوزاری دەرەکی وەک healthchecks.io کاتێک دەنگ نەما ئاگادارت دەکاتەوە، بۆیە وەستانی بێدەنگی GitHub cron ـیش دەگرێتەوە. کاتێک بڵاوکردنەوە بە ئەنقەست ناچالاک دەکەیت، پشکنینی heartbeat لای خزمەتگوزاری pause بکە بۆ ئەوەی ئاگاداری هەڵە نەبێت.

داشبۆرد بەشی «کارایی و داتا»ی هەیە کە سێ شت نیشان دەدات لە تۆمارەکەوە: retention بۆ هەر ڤیدیۆ (بەرزترین بۆ نزمترین)، بەراوردی جۆرەکانی کۆمێنتی CTA، و بەستەری playlist ـە کاشکراوەکان. ئەم داتایانە تەنها کاتێک پڕ دەبنەوە کە scope ـی analytics چالاک بێت و ڕاپۆرتی ڕۆژانە جێبەجێ بووبێت؛ تا ئەو کاتە پەیامی empty-state ـی ڕوون دەردەکەوێت.

چاکسازییەکان تەنها لەم کۆپییە ناوخۆییەدان تا repository و داشبۆردی میوانکراو نوێ بکرێنەوە.


## Backup timer for posting delays

GitHub's scheduled events are best-effort and can be delayed or dropped under load. To reduce long gaps, the dashboard also has a restricted endpoint for a free external timer. This endpoint can only ask the existing workflow to process a due scheduled slot; it cannot request manual publishing, choose a batch size, or bypass the ledger and upload safety checks.

1. In Vercel → Project → Settings → Environment Variables, add `SCHEDULE_TRIGGER_KEY` as a Production secret. Use a new long random value, separate from `DASHBOARD_KEY`, then redeploy Production.
2. In cron-job.org, create three HTTPS POST jobs for `https://quran-shorts-bot.vercel.app/api/schedule-trigger`, at 06:00, 12:00, and 18:00 in the `Asia/Baghdad` timezone.
3. For each job, set `Content-Type: application/json` and `X-Schedule-Key: [the same SCHEDULE_TRIGGER_KEY]`; set the body to `{"trigger":"scheduled"}`.

The timer receives a short accepted/no-due response after asking GitHub to start the existing workflow. GitHub still performs video creation and upload. The original GitHub schedule remains enabled as a backup; the durable ledger and workflow concurrency guard prevent duplicate scheduled uploads. Both services are best-effort, so this reduces the chance of a multi-hour delay but cannot guarantee an exact publish minute.
