CREATE TABLE nodes(
      node_id TEXT PRIMARY KEY,
      last_seen TEXT,
      status TEXT
    , ip TEXT, version TEXT, free_space_pct REAL, queue_len INTEGER);
CREATE TABLE clips(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      node_id TEXT,
      filepath TEXT,
      timestamp TEXT
    );
CREATE TABLE sqlite_sequence(name,seq);
