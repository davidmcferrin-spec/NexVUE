CREATE INDEX idx_input_status_dev_ts ON input_status(device_index, ts);

CREATE INDEX idx_samples_channel_ts ON samples(channel, ts);

CREATE INDEX idx_samples_ts ON samples(ts);

CREATE INDEX idx_viewer_sessions_channel ON viewer_sessions(channel);

CREATE INDEX idx_viewer_sessions_last_seen ON viewer_sessions(last_seen);

CREATE INDEX idx_viewer_sessions_remote ON viewer_sessions(remote_addr);

CREATE TABLE host_samples (
                ts INTEGER NOT NULL PRIMARY KEY,
                cpu_pct REAL,
                mem_used_bytes INTEGER,
                mem_total_bytes INTEGER,
                load1 REAL
            , gpu_video_pct REAL, gpu_render_pct REAL, gpu_video_enhance_pct REAL, gpu_freq_mhz REAL, cpu_temp_c REAL, gpu_temp_c REAL);

CREATE TABLE input_status (
                ts INTEGER NOT NULL,
                device_index INTEGER NOT NULL,
                card_name TEXT,
                input_locked INTEGER,
                input_mode TEXT,
                reference_locked INTEGER,
                reference_mode TEXT
            );

CREATE TABLE samples (
                ts INTEGER NOT NULL,
                channel TEXT NOT NULL,
                bandwidth_bps REAL,
                readers INTEGER,
                ready INTEGER
            );

CREATE TABLE totals (
                ts INTEGER NOT NULL PRIMARY KEY,
                active_streams INTEGER,
                total_readers INTEGER,
                total_bandwidth_bps REAL
            );

CREATE TABLE viewer_sessions (
                session_id TEXT PRIMARY KEY,
                remote_addr TEXT,
                channel TEXT,
                user_agent TEXT,
                first_seen INTEGER,
                last_seen INTEGER,
                bytes_sent INTEGER
            , user TEXT);
