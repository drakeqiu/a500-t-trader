# Historical daily-history jobs use calendar-quarter chunks

Status: Accepted

Historical daily-history jobs split the requested inclusive date range at calendar-quarter boundaries, clipping the first and last chunks to the requested range. Calendar-quarter chunks replace calendar-year chunks to provide finer progress updates without the request count of monthly chunks. The task list displays a progress bar for each job together with completed/total chunks and percentage; progress advances only after a chunk is checkpointed, with no estimate of progress within a chunk. This is informational job progress, not a task-owned market-data result. Preserve the existing all-or-nothing publication contract: failed or cancelled jobs do not publish partial history.