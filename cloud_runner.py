try:
        max_candidates = max(30, int(os.environ.get('QURAN_MAX_CANDIDATES', '180')))
    except (TypeError, ValueError):
        max_candidates = 180
    try:
        search_timeout = max(60.0, float(os.environ.get('QURAN_SEARCH_TIMEOUT_SECONDS', '480')))
    except (TypeError, ValueError):
        search_timeout = 480.0
    candidate_limit = min(total_verses * len(english), max_candidates)
    deadline = time.monotonic() + search_timeout
    for attempt in range(candidate_limit):
        if time.monotonic() >= deadline:
            raise CloudError('Quran Foundation verse search timed out; the next run will retry safely')
        if attempt == 0 or attempt % 10 == 0:
            print(f'Checking complete-verse candidate {attempt + 1}/{candidate_limit}...', flush=True)
