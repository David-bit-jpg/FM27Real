#pragma once

// Starts the local query API (127.0.0.1:27270) on a background thread and
// writes its URL and token to api.json in the data folder.
void http_start(void);
