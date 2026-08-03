<?php
/**
 * nexvue-auth-lib.php — local auth helpers (stdlib PHP + openssl, no Composer).
 *
 * SQLite: /var/lib/nexvue/auth/auth.db (override NEXVUE_AUTH_DB)
 * Keys:   /var/lib/nexvue/auth/{private.pem,public.pem,jwks.json,kid}
 *         (override NEXVUE_AUTH_DIR)
 *
 * DB lives inside the www-data-owned auth dir so SQLite WAL sidecars
 * (auth.db-wal / auth.db-shm) can be created under Apache. Legacy path
 * /var/lib/nexvue/auth.db is migrated on open when possible.
 *
 * Roles: admin | operator | sharer | viewer
 * Share links: named, channel-scoped, mandatory expires_at, revocable,
 * hard-deletable; expired rows purged 7 days after expires_at.
 * Raw share token is stored (token column) so admins/sharers can re-copy the
 * same URL; revoke/delete/expiry remain the access controls.
 * User channel ACL: users.channels JSON (NULL = all ch0–ch7).
 */

declare(strict_types=1);

const NEXVUE_AUTH_ROLES = ['admin', 'operator', 'sharer', 'viewer'];
const NEXVUE_AUTH_JWT_TTL_S = 90;
const NEXVUE_AUTH_PUBLISH_TTL_S = 315360000; // ~10 years
const NEXVUE_AUTH_RESET_TTL_S = 3600;
const NEXVUE_AUTH_MAX_CHANNELS = 8; // ch0..ch7 (+ lo)
/** Keep expired share rows this long after expires_at, then hard-delete. */
const NEXVUE_AUTH_SHARE_PURGE_GRACE_S = 604800; // 7 days
const NEXVUE_AUTH_SCHEMA_VERSION = 3;

function auth_dir(): string {
    $o = getenv('NEXVUE_AUTH_DIR');
    if (is_string($o) && $o !== '') {
        return rtrim($o, '/\\');
    }
    return '/var/lib/nexvue/auth';
}

function auth_db_path(): string {
    $o = getenv('NEXVUE_AUTH_DB');
    if (is_string($o) && $o !== '') {
        return $o;
    }
    $preferred = auth_dir() . '/auth.db';
    $legacy = '/var/lib/nexvue/auth.db';
    if (!is_file($preferred) && is_file($legacy)) {
        $dir = auth_dir();
        if (is_dir($dir) && is_writable($dir) && @rename($legacy, $preferred)) {
            foreach (glob($legacy . '-*') ?: [] as $side) {
                if (is_string($side) && is_file($side)) {
                    @rename($side, $dir . '/' . basename($side));
                }
            }
            return $preferred;
        }
        return $legacy;
    }
    return $preferred;
}

function auth_station_env_path(): string {
    $o = getenv('NEXVUE_STATION_ENV');
    if (is_string($o) && $o !== '') {
        return $o;
    }
    return '/etc/nexvue/nexvue.env';
}

function auth_b64url_encode(string $bin): string {
    return rtrim(strtr(base64_encode($bin), '+/', '-_'), '=');
}

function auth_b64url_decode(string $s): string|false {
    $pad = 4 - (strlen($s) % 4);
    if ($pad < 4) {
        $s .= str_repeat('=', $pad);
    }
    return base64_decode(strtr($s, '-_', '+/'), true);
}

function auth_uuid(): string {
    $b = random_bytes(16);
    $b[6] = chr((ord($b[6]) & 0x0f) | 0x40);
    $b[8] = chr((ord($b[8]) & 0x3f) | 0x80);
    $h = bin2hex($b);
    return sprintf(
        '%s-%s-%s-%s-%s',
        substr($h, 0, 8),
        substr($h, 8, 4),
        substr($h, 12, 4),
        substr($h, 16, 4),
        substr($h, 20, 12)
    );
}

function auth_now_iso(): string {
    return gmdate('Y-m-d\TH:i:s\Z');
}

function auth_hash_token(string $raw): string {
    return hash('sha256', $raw);
}

/** @return SQLite3 */
function auth_db(): SQLite3 {
    static $db = null;
    static $path = null;
    $p = auth_db_path();
    if ($db instanceof SQLite3 && $path === $p) {
        return $db;
    }
    $dir = dirname($p);
    if (!is_dir($dir)) {
        if (!@mkdir($dir, 0750, true) && !is_dir($dir)) {
            throw new RuntimeException('cannot create auth db dir: ' . $dir);
        }
    }
    $db = new SQLite3($p);
    $db->busyTimeout(5000);
    $db->exec('PRAGMA journal_mode=WAL');
    $db->exec('PRAGMA foreign_keys=ON');
    $path = $p;
    return $db;
}

function auth_migrate(): void {
    static $done = false;
    if ($done) {
        return;
    }
    $db = auth_db();
    // Hot path: status/metrics/ops hit this often — skip DDL once schema is stamped.
    $ver = (int)$db->querySingle('PRAGMA user_version');
    if ($ver >= NEXVUE_AUTH_SCHEMA_VERSION) {
        $done = true;
        return;
    }
    if ($ver < 1) {
        $db->exec(<<<'SQL'
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  email TEXT,
  role TEXT NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 0,
  disabled_at TEXT,
  channels TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  synced_at TEXT
);
CREATE TABLE IF NOT EXISTS share_links (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  token TEXT,
  page TEXT NOT NULL DEFAULT 'player',
  channels TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  created_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  synced_at TEXT
);
CREATE TABLE IF NOT EXISTS password_resets (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_share_expires ON share_links(expires_at);
CREATE INDEX IF NOT EXISTS idx_users_updated ON users(updated_at);
CREATE INDEX IF NOT EXISTS idx_shares_updated ON share_links(updated_at);
SQL);
        $n = (int)$db->querySingle('SELECT COUNT(*) FROM users');
        if ($n === 0) {
            auth_user_create([
                'username' => 'admin',
                'password' => 'password',
                'email' => null,
                'role' => 'admin',
                'must_change_password' => true,
            ]);
        }
        $ver = 1;
    }
    if ($ver < 2) {
        $cols = [];
        $info = $db->query('PRAGMA table_info(users)');
        if ($info) {
            while ($c = $info->fetchArray(SQLITE3_ASSOC)) {
                $cols[(string)$c['name']] = true;
            }
        }
        if (!isset($cols['channels'])) {
            $db->exec('ALTER TABLE users ADD COLUMN channels TEXT');
        }
        $ver = 2;
    }
    if ($ver < 3) {
        $cols = [];
        $info = $db->query('PRAGMA table_info(share_links)');
        if ($info) {
            while ($c = $info->fetchArray(SQLITE3_ASSOC)) {
                $cols[(string)$c['name']] = true;
            }
        }
        if (!isset($cols['token'])) {
            $db->exec('ALTER TABLE share_links ADD COLUMN token TEXT');
        }
        if (!isset($cols['page'])) {
            $db->exec("ALTER TABLE share_links ADD COLUMN page TEXT NOT NULL DEFAULT 'player'");
        }
        $ver = 3;
    }
    $db->exec('PRAGMA user_version = ' . (string)NEXVUE_AUTH_SCHEMA_VERSION);
    $done = true;
}

function auth_ensure_keys(): array {
    static $cached = null;
    if (is_array($cached)) {
        return $cached;
    }

    $dir = auth_dir();
    if (!is_dir($dir)) {
        if (!@mkdir($dir, 0750, true) && !is_dir($dir)) {
            throw new RuntimeException('cannot create auth dir: ' . $dir);
        }
    }
    $privPath = $dir . '/private.pem';
    $pubPath = $dir . '/public.pem';
    $kidPath = $dir . '/kid';
    $jwksPath = $dir . '/jwks.json';

    // Fast path: existing key material + JWKS — no openssl rebuild per request.
    if (is_file($privPath) && is_file($pubPath) && is_file($jwksPath)) {
        $privPem = (string)file_get_contents($privPath);
        $pubPem = (string)file_get_contents($pubPath);
        $kid = is_file($kidPath) ? trim((string)file_get_contents($kidPath)) : '';
        $jwksRaw = trim((string)file_get_contents($jwksPath));
        $jwks = json_decode($jwksRaw, true);
        if ($privPem !== '' && $pubPem !== '' && is_array($jwks) && isset($jwks['keys'][0])) {
            if ($kid === '' && isset($jwks['keys'][0]['kid']) && is_string($jwks['keys'][0]['kid'])) {
                $kid = $jwks['keys'][0]['kid'];
            }
            if ($kid !== '') {
                $cached = [
                    'private_pem' => $privPem,
                    'public_pem' => $pubPem,
                    'kid' => $kid,
                    'jwks' => $jwks,
                    'jwks_path' => $jwksPath,
                ];
                return $cached;
            }
        }
    }

    if (!is_file($privPath) || !is_file($pubPath)) {
        $cfg = [
            'private_key_type' => OPENSSL_KEYTYPE_RSA,
            'private_key_bits' => 2048,
        ];
        $res = openssl_pkey_new($cfg);
        if ($res === false) {
            throw new RuntimeException('openssl_pkey_new failed');
        }
        openssl_pkey_export($res, $privPem);
        $details = openssl_pkey_get_details($res);
        if ($details === false || empty($details['key'])) {
            throw new RuntimeException('openssl_pkey_get_details failed');
        }
        $pubPem = $details['key'];
        if (file_put_contents($privPath, $privPem) === false
            || file_put_contents($pubPath, $pubPem) === false) {
            throw new RuntimeException('cannot write auth keypair');
        }
        @chmod($privPath, 0640);
        @chmod($pubPath, 0644);
        $kid = auth_b64url_encode(random_bytes(8));
        file_put_contents($kidPath, $kid);
    }

    $kid = is_file($kidPath) ? trim((string)file_get_contents($kidPath)) : '';
    if ($kid === '') {
        $kid = auth_b64url_encode(random_bytes(8));
        file_put_contents($kidPath, $kid);
    }

    $privPem = (string)file_get_contents($privPath);
    $pubPem = (string)file_get_contents($pubPath);
    if ($privPem === '' || $pubPem === '') {
        throw new RuntimeException('auth keypair unreadable');
    }
    $jwks = auth_build_jwks($pubPem, $kid);
    $jwksJson = json_encode($jwks, JSON_UNESCAPED_SLASHES);
    if ($jwksJson === false) {
        throw new RuntimeException('jwks encode failed');
    }
    $needWrite = !is_file($jwksPath)
        || trim((string)file_get_contents($jwksPath)) !== $jwksJson;
    if ($needWrite && file_put_contents($jwksPath, $jwksJson) === false) {
        if (!is_file($jwksPath)) {
            throw new RuntimeException('cannot write jwks.json');
        }
    }

    $cached = [
        'private_pem' => $privPem,
        'public_pem' => $pubPem,
        'kid' => $kid,
        'jwks' => $jwks,
        'jwks_path' => $jwksPath,
    ];
    return $cached;
}

function auth_build_jwks(string $pubPem, string $kid): array {
    $res = openssl_pkey_get_public($pubPem);
    if ($res === false) {
        throw new RuntimeException('invalid public key');
    }
    $details = openssl_pkey_get_details($res);
    if ($details === false || ($details['type'] ?? null) !== OPENSSL_KEYTYPE_RSA) {
        throw new RuntimeException('expected RSA public key');
    }
    $n = $details['rsa']['n'];
    $e = $details['rsa']['e'];
    return [
        'keys' => [[
            'kty' => 'RSA',
            'use' => 'sig',
            'alg' => 'RS256',
            'kid' => $kid,
            'n' => auth_b64url_encode($n),
            'e' => auth_b64url_encode($e),
        ]],
    ];
}

function auth_jwt_encode(array $claims, ?int $ttlS = null): string {
    $keys = auth_ensure_keys();
    $now = time();
    $ttl = $ttlS ?? NEXVUE_AUTH_JWT_TTL_S;
    $payload = array_merge([
        'iat' => $now,
        'exp' => $now + $ttl,
        'iss' => 'nexvue-edge',
    ], $claims);
    $header = ['alg' => 'RS256', 'typ' => 'JWT', 'kid' => $keys['kid']];
    $h = auth_b64url_encode(json_encode($header, JSON_UNESCAPED_SLASHES));
    $p = auth_b64url_encode(json_encode($payload, JSON_UNESCAPED_SLASHES));
    $data = $h . '.' . $p;
    $sig = '';
    $ok = openssl_sign($data, $sig, $keys['private_pem'], OPENSSL_ALGO_SHA256);
    if (!$ok) {
        throw new RuntimeException('openssl_sign failed');
    }
    return $data . '.' . auth_b64url_encode($sig);
}

/** Normalize channel path list: chN base ids → include lo companions for WHEP. */
function auth_expand_channel_paths(array $channels): array {
    $out = [];
    foreach ($channels as $c) {
        if (!is_string($c)) {
            continue;
        }
        $c = strtolower(trim($c));
        if (!preg_match('/^ch[0-7](lo)?$/', $c)) {
            continue;
        }
        if (str_ends_with($c, 'lo')) {
            $base = substr($c, 0, -2);
            $out[$base] = true;
            $out[$c] = true;
        } else {
            $out[$c] = true;
            $out[$c . 'lo'] = true;
        }
    }
    return array_keys($out);
}

function auth_all_channel_paths(): array {
    $out = [];
    for ($i = 0; $i < NEXVUE_AUTH_MAX_CHANNELS; $i++) {
        $out[] = 'ch' . $i;
        $out[] = 'ch' . $i . 'lo';
    }
    return $out;
}

function auth_permissions_for_paths(array $paths, string $action = 'read'): array {
    $perms = [];
    foreach (auth_expand_channel_paths($paths) as $path) {
        $perms[] = ['action' => $action, 'path' => $path];
    }
    return $perms;
}

function auth_mint_viewer_jwt(string $sub, array $channelBases, ?int $ttlS = null): string {
    $perms = auth_permissions_for_paths($channelBases, 'read');
    if ($perms === []) {
        throw new InvalidArgumentException('no channels for JWT');
    }
    return auth_jwt_encode([
        'sub' => $sub,
        'mediamtx_permissions' => $perms,
    ], $ttlS);
}

function auth_mint_publish_jwt(): string {
    return auth_jwt_encode([
        'sub' => 'nexvue-encode',
        'mediamtx_permissions' => [
            ['action' => 'publish', 'path' => ''],
            ['action' => 'api'],
        ],
    ], NEXVUE_AUTH_PUBLISH_TTL_S);
}

/**
 * Read a KEY=value from nexvue.env (unquoted / simple quotes).
 */
function auth_read_env_key(string $name): string {
    $env = getenv($name);
    if (is_string($env) && $env !== '') {
        return $env;
    }
    $path = auth_station_env_path();
    if (!is_readable($path)) {
        return '';
    }
    $raw = (string)file_get_contents($path);
    $re = '/^\s*' . preg_quote($name, '/') . '=(.*)$/m';
    if (preg_match($re, $raw, $m)) {
        $v = trim($m[1]);
        if (
            (str_starts_with($v, '"') && str_ends_with($v, '"'))
            || (str_starts_with($v, "'") && str_ends_with($v, "'"))
        ) {
            $v = substr($v, 1, -1);
        }
        return $v;
    }
    return '';
}

/**
 * Station API / sync key. Prefers NEXVUE_API_KEY; falls back to NEXVUE_SYNC_KEY
 * (legacy name — same credential for portal sync and edge API Bearer auth).
 */
function auth_read_api_key(): string {
    $k = auth_read_env_key('NEXVUE_API_KEY');
    if ($k !== '') {
        return $k;
    }
    return auth_read_env_key('NEXVUE_SYNC_KEY');
}

/** @deprecated use auth_read_api_key() */
function auth_read_sync_key(): string {
    return auth_read_api_key();
}

function auth_read_publish_jwt_from_env(): string {
    $env = getenv('NEXVUE_PUBLISH_JWT');
    if (is_string($env) && $env !== '') {
        return $env;
    }
    $path = auth_station_env_path();
    if (!is_readable($path)) {
        return '';
    }
    $raw = (string)file_get_contents($path);
    if (preg_match('/^\s*NEXVUE_PUBLISH_JWT=(.*)$/m', $raw, $m)) {
        return trim($m[1]);
    }
    return '';
}

/**
 * Ensure NEXVUE_PUBLISH_JWT is present in station env file. Returns the JWT.
 */
function auth_ensure_publish_jwt_in_env(): string {
    $existing = auth_read_publish_jwt_from_env();
    if ($existing !== '') {
        return $existing;
    }
    auth_ensure_keys();
    $jwt = auth_mint_publish_jwt();
    $path = auth_station_env_path();
    $dir = dirname($path);
    if (!is_dir($dir)) {
        @mkdir($dir, 0755, true);
    }
    $line = 'NEXVUE_PUBLISH_JWT=' . $jwt . "\n";
    if (is_file($path)) {
        $raw = (string)file_get_contents($path);
        if (preg_match('/^\s*NEXVUE_PUBLISH_JWT=/m', $raw)) {
            $raw = preg_replace('/^\s*NEXVUE_PUBLISH_JWT=.*$/m', 'NEXVUE_PUBLISH_JWT=' . $jwt, $raw, 1);
            file_put_contents($path, $raw);
        } else {
            if ($raw !== '' && !str_ends_with($raw, "\n")) {
                $raw .= "\n";
            }
            file_put_contents($path, $raw . "\n# Long-lived MediaMTX publish+api JWT (local auth)\n" . $line);
        }
    } else {
        file_put_contents($path, "# Generated by nexvue-auth-bootstrap\n" . $line);
    }
    putenv('NEXVUE_PUBLISH_JWT=' . $jwt);
    return $jwt;
}

function auth_session_start(): void {
    if (session_status() === PHP_SESSION_ACTIVE) {
        return;
    }
    $secure = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
        || (isset($_SERVER['SERVER_PORT']) && (int)$_SERVER['SERVER_PORT'] === 443);
    session_name('nexvue_session');
    session_set_cookie_params([
        'lifetime' => 0,
        'path' => '/',
        'secure' => $secure,
        'httponly' => true,
        'samesite' => 'Lax',
    ]);
    session_start();
}

/**
 * Release the session lock so concurrent Player polls (status / WHEP JWT /
 * captions SSE) do not serialize on the same PHP session file.
 * Call after read-only auth checks on hot endpoints.
 */
function auth_session_release(): void {
    if (session_status() === PHP_SESSION_ACTIVE) {
        session_write_close();
    }
}

/** How long to trust session-cached identity without re-hitting SQLite. */
const NEXVUE_AUTH_SESSION_CACHE_S = 60;

function auth_session_clear(): void {
    auth_session_start();
    $_SESSION = [];
    if (ini_get('session.use_cookies')) {
        $p = session_get_cookie_params();
        setcookie(session_name(), '', [
            'expires' => time() - 42000,
            'path' => $p['path'],
            'secure' => $p['secure'],
            'httponly' => $p['httponly'],
            'samesite' => $p['samesite'] ?? 'Lax',
        ]);
    }
    session_destroy();
}

function auth_normalize_username(string $u): string {
    $u = trim($u);
    if ($u === '' || strlen($u) > 64 || !preg_match('/^[A-Za-z0-9._@+-]+$/', $u)) {
        throw new InvalidArgumentException('invalid username');
    }
    return $u;
}

function auth_normalize_email(?string $email): ?string {
    if ($email === null) {
        return null;
    }
    $email = trim($email);
    if ($email === '') {
        return null;
    }
    if (strlen($email) > 254 || !filter_var($email, FILTER_VALIDATE_EMAIL)) {
        throw new InvalidArgumentException('invalid email');
    }
    return $email;
}

function auth_normalize_role(string $role): string {
    $role = strtolower(trim($role));
    if (!in_array($role, NEXVUE_AUTH_ROLES, true)) {
        throw new InvalidArgumentException('invalid role');
    }
    return $role;
}

/**
 * Normalize channel bases (ch0–ch7). Dedupe; optionally sort (default).
 * Pass $sort=false for share links so Multiview auto-tune keeps pane order.
 *
 * @param list<mixed> $channels
 * @return list<string>
 */
function auth_normalize_channels(array $channels, bool $sort = true): array {
    $bases = [];
    foreach ($channels as $c) {
        if (!is_string($c)) {
            continue;
        }
        $c = strtolower(trim($c));
        if (str_ends_with($c, 'lo')) {
            $c = substr($c, 0, -2);
        }
        if (!preg_match('/^ch[0-7]$/', $c)) {
            throw new InvalidArgumentException('invalid channel: ' . $c);
        }
        $bases[$c] = true;
    }
    $list = array_keys($bases);
    if ($sort) {
        sort($list, SORT_NATURAL);
    }
    if ($list === []) {
        throw new InvalidArgumentException('at least one channel required');
    }
    return $list;
}

/** Max channels on a Multiview share link (matches dual/quad pane count). */
const NEXVUE_AUTH_MULTIVIEW_SHARE_MAX = 4;

/**
 * Normalize share channels; Multiview page capped at NEXVUE_AUTH_MULTIVIEW_SHARE_MAX.
 *
 * @param list<mixed> $channels
 * @return list<string>
 */
function auth_normalize_share_channels(array $channels, string $page = 'player'): array {
    $page = ($page === 'multiview') ? 'multiview' : 'player';
    // Preserve submission order for Multiview pane auto-tune.
    $list = auth_normalize_channels($channels, false);
    if ($page === 'multiview' && count($list) > NEXVUE_AUTH_MULTIVIEW_SHARE_MAX) {
        throw new InvalidArgumentException(
            'multiview shares allow at most ' . NEXVUE_AUTH_MULTIVIEW_SHARE_MAX . ' channels'
        );
    }
    return $list;
}

/** Base channel paths ch0..ch7 (no lo). */
function auth_all_channel_bases(): array {
    $out = [];
    for ($i = 0; $i < NEXVUE_AUTH_MAX_CHANNELS; $i++) {
        $out[] = 'ch' . $i;
    }
    return $out;
}

/**
 * Encode user channel ACL for SQLite.
 * null / full ch0–ch7 set → NULL (all channels).
 *
 * @param list<string>|null $channels
 */
function auth_encode_user_channels(?array $channels): ?string {
    if ($channels === null) {
        return null;
    }
    $list = auth_normalize_channels($channels);
    if ($list === auth_all_channel_bases()) {
        return null;
    }
    return json_encode(array_values($list), JSON_UNESCAPED_SLASHES);
}

/**
 * Decode users.channels column.
 * null / empty → null meaning "all channels".
 *
 * @return list<string>|null
 */
function auth_decode_user_channels(mixed $raw): ?array {
    if ($raw === null || $raw === '') {
        return null;
    }
    if (is_array($raw)) {
        return auth_normalize_channels($raw);
    }
    if (!is_string($raw)) {
        return null;
    }
    $decoded = json_decode($raw, true);
    if (!is_array($decoded) || $decoded === []) {
        return null;
    }
    return auth_normalize_channels($decoded);
}

/**
 * Effective base channels for a user row (never empty).
 *
 * @return list<string>
 */
function auth_user_channel_bases(array $row): array {
    $decoded = auth_decode_user_channels($row['channels'] ?? null);
    return $decoded ?? auth_all_channel_bases();
}

/**
 * True when every channel in $want is allowed for $row.
 *
 * @param list<string> $want
 */
function auth_user_allows_channels(array $row, array $want): bool {
    $allowed = array_fill_keys(auth_user_channel_bases($row), true);
    foreach (auth_normalize_channels($want) as $c) {
        if (!isset($allowed[$c])) {
            return false;
        }
    }
    return true;
}

function auth_parse_expires(?string $absolute, ?int $durationS): string {
    if ($absolute !== null && trim($absolute) !== '') {
        $t = strtotime(trim($absolute));
        if ($t === false) {
            throw new InvalidArgumentException('invalid expires_at');
        }
        if ($t <= time()) {
            throw new InvalidArgumentException('expires_at must be in the future');
        }
        return gmdate('Y-m-d\TH:i:s\Z', $t);
    }
    if ($durationS !== null && $durationS > 0) {
        return gmdate('Y-m-d\TH:i:s\Z', time() + $durationS);
    }
    throw new InvalidArgumentException('expiry required (absolute or duration)');
}

function auth_user_row_public(array $row): array {
    $channels = auth_decode_user_channels($row['channels'] ?? null);
    return [
        'id' => $row['id'],
        'username' => $row['username'],
        'email' => $row['email'] !== null && $row['email'] !== '' ? $row['email'] : null,
        'role' => $row['role'],
        'must_change_password' => ((int)$row['must_change_password']) === 1,
        'disabled_at' => $row['disabled_at'] ?: null,
        // null = all station channels; otherwise restricted base list.
        'channels' => $channels,
        'created_at' => $row['created_at'],
        'updated_at' => $row['updated_at'],
        'synced_at' => $row['synced_at'] ?: null,
    ];
}

function auth_user_find_by_username(string $username): ?array {
    $db = auth_db();
    $st = $db->prepare('SELECT * FROM users WHERE username = :u COLLATE NOCASE LIMIT 1');
    $st->bindValue(':u', $username, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function auth_user_find_by_id(string $id): ?array {
    $db = auth_db();
    $st = $db->prepare('SELECT * FROM users WHERE id = :id LIMIT 1');
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function auth_user_find_by_email(string $email): ?array {
    $db = auth_db();
    $st = $db->prepare('SELECT * FROM users WHERE email = :e COLLATE NOCASE LIMIT 1');
    $st->bindValue(':e', $email, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function auth_user_create(array $in): array {
    $username = auth_normalize_username((string)($in['username'] ?? ''));
    $role = auth_normalize_role((string)($in['role'] ?? 'viewer'));
    $email = auth_normalize_email(isset($in['email']) ? (string)$in['email'] : null);
    $must = !empty($in['must_change_password']) ? 1 : 0;
    $id = isset($in['id']) && is_string($in['id']) && $in['id'] !== ''
        ? $in['id']
        : auth_uuid();
    $now = auth_now_iso();
    if (isset($in['password_hash']) && is_string($in['password_hash']) && $in['password_hash'] !== '') {
        $hash = $in['password_hash'];
    } else {
        $password = (string)($in['password'] ?? '');
        if (strlen($password) < 8) {
            throw new InvalidArgumentException('password must be at least 8 characters');
        }
        $hash = password_hash($password, PASSWORD_BCRYPT);
    }
    $channelsSql = null;
    if (array_key_exists('channels', $in)) {
        if ($in['channels'] === null) {
            $channelsSql = null;
        } elseif (is_array($in['channels'])) {
            $channelsSql = auth_encode_user_channels($in['channels']);
        } elseif (is_string($in['channels']) && $in['channels'] !== '') {
            $channelsSql = auth_encode_user_channels(
                auth_decode_user_channels($in['channels']) ?? auth_all_channel_bases()
            );
        }
    }
    $db = auth_db();
    $st = $db->prepare(
        'INSERT INTO users (id, username, password_hash, email, role, must_change_password, disabled_at, channels, created_at, updated_at, synced_at)
         VALUES (:id, :u, :ph, :e, :r, :m, :d, :ch, :c, :up, :sy)'
    );
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->bindValue(':u', $username, SQLITE3_TEXT);
    $st->bindValue(':ph', $hash, SQLITE3_TEXT);
    $st->bindValue(':e', $email, $email === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':r', $role, SQLITE3_TEXT);
    $st->bindValue(':m', $must, SQLITE3_INTEGER);
    $st->bindValue(':d', $in['disabled_at'] ?? null, isset($in['disabled_at']) && $in['disabled_at'] ? SQLITE3_TEXT : SQLITE3_NULL);
    $st->bindValue(':ch', $channelsSql, $channelsSql === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':c', $in['created_at'] ?? $now, SQLITE3_TEXT);
    $st->bindValue(':up', $in['updated_at'] ?? $now, SQLITE3_TEXT);
    $st->bindValue(':sy', $in['synced_at'] ?? null, isset($in['synced_at']) && $in['synced_at'] ? SQLITE3_TEXT : SQLITE3_NULL);
    if (!$st->execute()) {
        throw new RuntimeException('user create failed (username taken?)');
    }
    $row = auth_user_find_by_id($id);
    if ($row === null) {
        throw new RuntimeException('user create failed');
    }
    return $row;
}

function auth_user_update(string $id, array $in): array {
    $row = auth_user_find_by_id($id);
    if ($row === null) {
        throw new InvalidArgumentException('user not found');
    }
    $username = array_key_exists('username', $in)
        ? auth_normalize_username((string)$in['username'])
        : $row['username'];
    $role = array_key_exists('role', $in)
        ? auth_normalize_role((string)$in['role'])
        : $row['role'];
    $email = array_key_exists('email', $in)
        ? auth_normalize_email($in['email'] === null || $in['email'] === '' ? null : (string)$in['email'])
        : ($row['email'] !== '' ? $row['email'] : null);
    $must = array_key_exists('must_change_password', $in)
        ? (!empty($in['must_change_password']) ? 1 : 0)
        : (int)$row['must_change_password'];
    $disabled = array_key_exists('disabled', $in)
        ? (!empty($in['disabled']) ? auth_now_iso() : null)
        : ($row['disabled_at'] ?: null);
    if (array_key_exists('disabled_at', $in)) {
        $disabled = $in['disabled_at'] ? (string)$in['disabled_at'] : null;
    }
    $hash = $row['password_hash'];
    if (!empty($in['password'])) {
        $pw = (string)$in['password'];
        if (strlen($pw) < 8) {
            throw new InvalidArgumentException('password must be at least 8 characters');
        }
        $hash = password_hash($pw, PASSWORD_BCRYPT);
        $must = array_key_exists('must_change_password', $in) ? $must : 0;
    }
    if (!empty($in['password_hash']) && is_string($in['password_hash'])) {
        $hash = $in['password_hash'];
    }
    $channelsSql = $row['channels'] ?? null;
    if (array_key_exists('channels', $in)) {
        if ($in['channels'] === null) {
            $channelsSql = null;
        } elseif (is_array($in['channels'])) {
            $channelsSql = auth_encode_user_channels($in['channels']);
        } elseif (is_string($in['channels'])) {
            $channelsSql = $in['channels'] === ''
                ? null
                : auth_encode_user_channels(
                    auth_decode_user_channels($in['channels']) ?? auth_all_channel_bases()
                );
        }
    }
    $now = auth_now_iso();
    $db = auth_db();
    $st = $db->prepare(
        'UPDATE users SET username=:u, password_hash=:ph, email=:e, role=:r,
         must_change_password=:m, disabled_at=:d, channels=:ch, updated_at=:up, synced_at=:sy WHERE id=:id'
    );
    $st->bindValue(':u', $username, SQLITE3_TEXT);
    $st->bindValue(':ph', $hash, SQLITE3_TEXT);
    $st->bindValue(':e', $email, $email === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':r', $role, SQLITE3_TEXT);
    $st->bindValue(':m', $must, SQLITE3_INTEGER);
    $st->bindValue(':d', $disabled, $disabled === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':ch', $channelsSql, $channelsSql === null || $channelsSql === '' ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':up', $now, SQLITE3_TEXT);
    $st->bindValue(':sy', $in['synced_at'] ?? $row['synced_at'], isset($in['synced_at']) || $row['synced_at'] ? SQLITE3_TEXT : SQLITE3_NULL);
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    if (!$st->execute()) {
        throw new RuntimeException('user update failed');
    }
    $out = auth_user_find_by_id($id);
    if ($out === null) {
        throw new RuntimeException('user update failed');
    }
    return $out;
}

function auth_user_verify(string $username, string $password): ?array {
    $row = auth_user_find_by_username($username);
    if ($row === null) {
        return null;
    }
    if (!empty($row['disabled_at'])) {
        return null;
    }
    if (!password_verify($password, $row['password_hash'])) {
        return null;
    }
    return $row;
}

function auth_users_list(): array {
    $db = auth_db();
    $r = $db->query('SELECT * FROM users ORDER BY username COLLATE NOCASE');
    $out = [];
    while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
        $out[] = auth_user_row_public($row);
    }
    return $out;
}

function auth_share_page_key(?string $page): string {
    return ($page === 'multiview') ? 'multiview' : 'player';
}

function auth_share_page_path(string $pageKey): string {
    // Path front door (legacy *.html redirects still work).
    return auth_share_page_key($pageKey) === 'multiview' ? 'multiview' : 'player';
}

/**
 * Absolute share URL for a raw token (same URL for the life of the share).
 */
function auth_share_build_url(string $token, string $pageKey = 'player', ?string $scheme = null, ?string $host = null): string {
    if ($scheme === null || $scheme === '') {
        $scheme = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
    }
    if ($host === null || $host === '') {
        $host = $_SERVER['HTTP_HOST'] ?? 'localhost';
    }
    return $scheme . '://' . $host . '/' . auth_share_page_path($pageKey)
        . '?t=' . rawurlencode($token);
}

function auth_share_row_public(array $row, bool $includeToken = false, ?string $rawToken = null): array {
    $channels = json_decode((string)$row['channels'], true);
    if (!is_array($channels)) {
        $channels = [];
    }
    $status = 'active';
    if (!empty($row['revoked_at'])) {
        $status = 'revoked';
    } elseif (strtotime((string)$row['expires_at']) !== false && strtotime((string)$row['expires_at']) <= time()) {
        $status = 'expired';
    }
    $createdBy = $row['created_by'] ?: null;
    $createdByUsername = null;
    if (is_string($createdBy) && $createdBy !== '') {
        $owner = auth_user_find_by_id($createdBy);
        if ($owner !== null) {
            $createdByUsername = (string)$owner['username'];
        }
    }
    $pageKey = auth_share_page_key(isset($row['page']) ? (string)$row['page'] : 'player');
    $token = $rawToken;
    if ($token === null || $token === '') {
        $stored = isset($row['token']) ? (string)$row['token'] : '';
        if ($stored !== '') {
            $token = $stored;
        }
    }
    $out = [
        'id' => $row['id'],
        'name' => $row['name'],
        'channels' => $channels,
        'page' => $pageKey,
        'expires_at' => $row['expires_at'],
        'revoked_at' => $row['revoked_at'] ?: null,
        'status' => $status,
        'created_by' => $createdBy,
        'created_by_username' => $createdByUsername,
        'created_at' => $row['created_at'],
        'updated_at' => $row['updated_at'],
        'synced_at' => $row['synced_at'] ?: null,
    ];
    if ($token !== null && $token !== '') {
        $out['url'] = auth_share_build_url($token, $pageKey);
        if ($includeToken) {
            $out['token'] = $token;
        }
    }
    return $out;
}

/**
 * @param 'player'|'multiview' $pageKey
 */
function auth_share_create(string $name, array $channels, string $expiresAt, ?string $createdBy, string $pageKey = 'player'): array {
    $name = trim($name);
    if ($name === '' || strlen($name) > 128) {
        throw new InvalidArgumentException('invalid share name');
    }
    // Preserve order (caller may already have validated via auth_normalize_share_channels).
    $channels = auth_normalize_channels($channels, false);
    // Re-validate expiry is set (no open-ended).
    if ($expiresAt === '' || strtotime($expiresAt) === false) {
        throw new InvalidArgumentException('expires_at required');
    }
    if (strtotime($expiresAt) <= time()) {
        throw new InvalidArgumentException('expires_at must be in the future');
    }
    $pageKey = auth_share_page_key($pageKey);
    $raw = bin2hex(random_bytes(32));
    $id = auth_uuid();
    $now = auth_now_iso();
    $db = auth_db();
    $st = $db->prepare(
        'INSERT INTO share_links (id, name, token_hash, token, page, channels, expires_at, revoked_at, created_by, created_at, updated_at, synced_at)
         VALUES (:id, :n, :th, :tok, :pg, :ch, :ex, NULL, :cb, :c, :up, NULL)'
    );
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->bindValue(':n', $name, SQLITE3_TEXT);
    $st->bindValue(':th', auth_hash_token($raw), SQLITE3_TEXT);
    $st->bindValue(':tok', $raw, SQLITE3_TEXT);
    $st->bindValue(':pg', $pageKey, SQLITE3_TEXT);
    $st->bindValue(':ch', json_encode($channels, JSON_UNESCAPED_SLASHES), SQLITE3_TEXT);
    $st->bindValue(':ex', $expiresAt, SQLITE3_TEXT);
    $st->bindValue(':cb', $createdBy, $createdBy === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':c', $now, SQLITE3_TEXT);
    $st->bindValue(':up', $now, SQLITE3_TEXT);
    if (!$st->execute()) {
        throw new RuntimeException('share create failed');
    }
    $row = auth_share_find_by_id($id);
    if ($row === null) {
        throw new RuntimeException('share create failed');
    }
    return ['row' => $row, 'token' => $raw];
}

function auth_share_find_by_id(string $id): ?array {
    $db = auth_db();
    $st = $db->prepare('SELECT * FROM share_links WHERE id = :id LIMIT 1');
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function auth_share_find_by_token(string $raw): ?array {
    $db = auth_db();
    $st = $db->prepare('SELECT * FROM share_links WHERE token_hash = :th LIMIT 1');
    $st->bindValue(':th', auth_hash_token($raw), SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function auth_share_is_valid(?array $row): bool {
    if ($row === null) {
        return false;
    }
    if (!empty($row['revoked_at'])) {
        return false;
    }
    $exp = strtotime((string)$row['expires_at']);
    if ($exp === false || $exp <= time()) {
        return false;
    }
    return true;
}

function auth_share_revoke(string $id): array {
    $row = auth_share_find_by_id($id);
    if ($row === null) {
        throw new InvalidArgumentException('share not found');
    }
    $now = auth_now_iso();
    $db = auth_db();
    $st = $db->prepare('UPDATE share_links SET revoked_at=:r, updated_at=:up WHERE id=:id');
    $st->bindValue(':r', $now, SQLITE3_TEXT);
    $st->bindValue(':up', $now, SQLITE3_TEXT);
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->execute();
    $out = auth_share_find_by_id($id);
    if ($out === null) {
        throw new RuntimeException('share revoke failed');
    }
    return $out;
}

/**
 * Hard-delete a share row (token becomes invalid immediately).
 *
 * @throws InvalidArgumentException when missing
 */
function auth_share_delete(string $id): void {
    $row = auth_share_find_by_id($id);
    if ($row === null) {
        throw new InvalidArgumentException('share not found');
    }
    $db = auth_db();
    $st = $db->prepare('DELETE FROM share_links WHERE id = :id');
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    if (!$st->execute()) {
        throw new RuntimeException('share delete failed');
    }
}

/**
 * Update name / channels / expiry. Does not rotate token_hash.
 * Revoked shares cannot be edited (delete instead). Extending expires_at
 * on an expired-but-not-revoked row revives it.
 *
 * @param list<string> $channels already normalized bases
 * @throws InvalidArgumentException on validation failure
 */
function auth_share_update(string $id, string $name, array $channels, string $expiresAt): array {
    $row = auth_share_find_by_id($id);
    if ($row === null) {
        throw new InvalidArgumentException('share not found');
    }
    if (!empty($row['revoked_at'])) {
        throw new InvalidArgumentException('cannot edit a revoked share');
    }
    $name = trim($name);
    if ($name === '' || strlen($name) > 128) {
        throw new InvalidArgumentException('invalid share name');
    }
    $channels = auth_normalize_channels($channels, false);
    if ($expiresAt === '' || strtotime($expiresAt) === false) {
        throw new InvalidArgumentException('expires_at required');
    }
    if (strtotime($expiresAt) <= time()) {
        throw new InvalidArgumentException('expires_at must be in the future');
    }
    $now = auth_now_iso();
    $db = auth_db();
    $st = $db->prepare(
        'UPDATE share_links SET name=:n, channels=:ch, expires_at=:ex, updated_at=:up WHERE id=:id'
    );
    $st->bindValue(':n', $name, SQLITE3_TEXT);
    $st->bindValue(':ch', json_encode($channels, JSON_UNESCAPED_SLASHES), SQLITE3_TEXT);
    $st->bindValue(':ex', $expiresAt, SQLITE3_TEXT);
    $st->bindValue(':up', $now, SQLITE3_TEXT);
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    if (!$st->execute()) {
        throw new RuntimeException('share update failed');
    }
    $out = auth_share_find_by_id($id);
    if ($out === null) {
        throw new RuntimeException('share update failed');
    }
    return $out;
}

/**
 * Hard-delete shares whose expires_at is older than $graceS (default 7 days).
 * Opportunistic cleanup — no timer required.
 *
 * @return int rows deleted
 */
function auth_shares_purge_expired(?int $graceS = null): int {
    $grace = $graceS ?? NEXVUE_AUTH_SHARE_PURGE_GRACE_S;
    if ($grace < 0) {
        $grace = 0;
    }
    $cutoff = gmdate('Y-m-d\TH:i:s\Z', time() - $grace);
    $db = auth_db();
    $st = $db->prepare('DELETE FROM share_links WHERE expires_at < :cut');
    $st->bindValue(':cut', $cutoff, SQLITE3_TEXT);
    $st->execute();
    return $db->changes();
}

/** Admin may manage any share; others only their own. */
function auth_share_can_manage(array $shareRow, array $userRow): bool {
    if (($userRow['role'] ?? '') === 'admin') {
        return true;
    }
    $owner = (string)($shareRow['created_by'] ?? '');
    $uid = (string)($userRow['id'] ?? '');
    return $owner !== '' && $uid !== '' && hash_equals($owner, $uid);
}

/**
 * @param string|null $createdBy when set, only shares created by that user id
 * @return list<array<string,mixed>>
 */
function auth_shares_list(?string $createdBy = null): array {
    auth_shares_purge_expired();
    $db = auth_db();
    if ($createdBy !== null && $createdBy !== '') {
        $st = $db->prepare(
            'SELECT * FROM share_links WHERE created_by = :cb ORDER BY created_at DESC'
        );
        $st->bindValue(':cb', $createdBy, SQLITE3_TEXT);
        $r = $st->execute();
    } else {
        $r = $db->query('SELECT * FROM share_links ORDER BY created_at DESC');
    }
    $out = [];
    if ($r) {
        while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
            $out[] = auth_share_row_public($row);
        }
    }
    return $out;
}

function auth_reset_create(string $userId): array {
    $raw = bin2hex(random_bytes(32));
    $id = auth_uuid();
    $now = auth_now_iso();
    $exp = gmdate('Y-m-d\TH:i:s\Z', time() + NEXVUE_AUTH_RESET_TTL_S);
    $db = auth_db();
    // Invalidate prior unused resets for this user.
    $db->exec("UPDATE password_resets SET used_at='" . $db->escapeString($now) . "' WHERE user_id='" . $db->escapeString($userId) . "' AND used_at IS NULL");
    $st = $db->prepare(
        'INSERT INTO password_resets (id, user_id, token_hash, expires_at, used_at, created_at)
         VALUES (:id, :uid, :th, :ex, NULL, :c)'
    );
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->bindValue(':uid', $userId, SQLITE3_TEXT);
    $st->bindValue(':th', auth_hash_token($raw), SQLITE3_TEXT);
    $st->bindValue(':ex', $exp, SQLITE3_TEXT);
    $st->bindValue(':c', $now, SQLITE3_TEXT);
    $st->execute();
    return ['id' => $id, 'token' => $raw, 'expires_at' => $exp];
}

function auth_reset_find_valid(string $raw): ?array {
    $db = auth_db();
    $st = $db->prepare('SELECT * FROM password_resets WHERE token_hash = :th LIMIT 1');
    $st->bindValue(':th', auth_hash_token($raw), SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    if (!$row || !empty($row['used_at'])) {
        return null;
    }
    $exp = strtotime((string)$row['expires_at']);
    if ($exp === false || $exp <= time()) {
        return null;
    }
    return $row;
}

function auth_reset_consume(string $raw, string $newPassword): void {
    if (strlen($newPassword) < 8) {
        throw new InvalidArgumentException('password must be at least 8 characters');
    }
    $row = auth_reset_find_valid($raw);
    if ($row === null) {
        throw new InvalidArgumentException('invalid or expired reset token');
    }
    auth_user_update($row['user_id'], [
        'password' => $newPassword,
        'must_change_password' => false,
    ]);
    $now = auth_now_iso();
    $db = auth_db();
    $st = $db->prepare('UPDATE password_resets SET used_at=:u WHERE id=:id');
    $st->bindValue(':u', $now, SQLITE3_TEXT);
    $st->bindValue(':id', $row['id'], SQLITE3_TEXT);
    $st->execute();
}

/**
 * Current logged-in user row (or null).
 *
 * @param bool $forceDb When true, skip the session snapshot (empty password_hash)
 *                      and request memo — always reload from SQLite and refresh
 *                      session fields. Required for change_password and any
 *                      path that needs a real password_hash or post-update flags.
 */
function auth_current_user(bool $forceDb = false): ?array {
    static $memo = null;
    static $memoSet = false;
    if ($forceDb) {
        $memo = null;
        $memoSet = false;
        auth_session_start();
        unset($_SESSION['user_cache_at']);
    }
    if ($memoSet) {
        return $memo;
    }
    auth_session_start();
    $uid = $_SESSION['user_id'] ?? null;
    if (!is_string($uid) || $uid === '') {
        $memoSet = true;
        $memo = null;
        return null;
    }
    $cacheAt = (int)($_SESSION['user_cache_at'] ?? 0);
    $role = $_SESSION['role'] ?? null;
    // Hot path (status/captions/WHEP): trust session snapshot briefly — avoids
    // SQLite on every 2s poll. Revalidate from DB when the cache ages out.
    // Snapshot intentionally omits password_hash (never put bcrypt in $_SESSION).
    // Callers that need the hash must pass $forceDb=true.
    // user_channels must be present (null = all-channels ACL); older sessions
    // without the key fall through to a DB reload.
    if (!$forceDb
        && is_string($role) && $role !== ''
        && $cacheAt >= (time() - NEXVUE_AUTH_SESSION_CACHE_S)
        && array_key_exists('user_channels', $_SESSION)) {
        $memo = [
            'id' => $uid,
            'username' => (string)($_SESSION['username'] ?? ''),
            'role' => $role,
            'password_hash' => '',
            'email' => null,
            'must_change_password' => !empty($_SESSION['must_change_password']) ? 1 : 0,
            'disabled_at' => null,
            // Raw DB value: null (all) or JSON string of base paths.
            'channels' => $_SESSION['user_channels'],
            'created_at' => '',
            'updated_at' => '',
            'synced_at' => null,
        ];
        $memoSet = true;
        return $memo;
    }
    $row = auth_user_find_by_id($uid);
    if ($row === null || !empty($row['disabled_at'])) {
        unset(
            $_SESSION['user_id'],
            $_SESSION['role'],
            $_SESSION['username'],
            $_SESSION['must_change_password'],
            $_SESSION['user_channels'],
            $_SESSION['user_cache_at']
        );
        $memoSet = true;
        $memo = null;
        return null;
    }
    $_SESSION['role'] = $row['role'];
    $_SESSION['username'] = $row['username'];
    $_SESSION['must_change_password'] = ((int)$row['must_change_password']) === 1;
    $_SESSION['user_channels'] = array_key_exists('channels', $row) ? $row['channels'] : null;
    $_SESSION['user_cache_at'] = time();
    $memo = $row;
    $memoSet = true;
    return $row;
}

function auth_current_share(): ?array {
    static $memo = null;
    static $memoSet = false;
    if ($memoSet) {
        return $memo;
    }
    auth_session_start();
    $sid = $_SESSION['share_id'] ?? null;
    if (!is_string($sid) || $sid === '') {
        $memoSet = true;
        $memo = null;
        return null;
    }
    $expiresAt = (string)($_SESSION['share_expires_at'] ?? '');
    if ($expiresAt !== '') {
        $t = strtotime($expiresAt);
        if ($t !== false && $t <= time()) {
            unset(
                $_SESSION['share_id'],
                $_SESSION['share_channels'],
                $_SESSION['share_expires_at'],
                $_SESSION['share_name'],
                $_SESSION['share_cache_at']
            );
            $memoSet = true;
            $memo = null;
            return null;
        }
    }
    $cacheAt = (int)($_SESSION['share_cache_at'] ?? 0);
    $channels = $_SESSION['share_channels'] ?? null;
    if (is_array($channels) && $cacheAt >= (time() - NEXVUE_AUTH_SESSION_CACHE_S)) {
        $memo = [
            'id' => $sid,
            'name' => (string)($_SESSION['share_name'] ?? ''),
            'channels' => json_encode($channels, JSON_UNESCAPED_SLASHES),
            'expires_at' => $expiresAt !== '' ? $expiresAt : gmdate('Y-m-d\TH:i:s\Z', time() + 3600),
            'revoked_at' => null,
            'token_hash' => '',
            'created_by' => null,
            'created_at' => '',
            'updated_at' => '',
            'synced_at' => null,
        ];
        $memoSet = true;
        return $memo;
    }
    $row = auth_share_find_by_id($sid);
    if (!auth_share_is_valid($row)) {
        unset(
            $_SESSION['share_id'],
            $_SESSION['share_channels'],
            $_SESSION['share_expires_at'],
            $_SESSION['share_name'],
            $_SESSION['share_cache_at']
        );
        $memoSet = true;
        $memo = null;
        return null;
    }
    $ch = json_decode((string)$row['channels'], true);
    $_SESSION['share_channels'] = is_array($ch) ? $ch : [];
    $_SESSION['share_expires_at'] = $row['expires_at'];
    $_SESSION['share_name'] = $row['name'];
    $_SESSION['share_cache_at'] = time();
    $memo = $row;
    $memoSet = true;
    return $row;
}

function auth_login_user(array $row): void {
    auth_session_start();
    session_regenerate_id(true);
    unset(
        $_SESSION['share_id'],
        $_SESSION['share_channels'],
        $_SESSION['share_expires_at'],
        $_SESSION['share_name'],
        $_SESSION['share_cache_at']
    );
    $_SESSION['user_id'] = $row['id'];
    $_SESSION['role'] = $row['role'];
    $_SESSION['username'] = $row['username'];
    $_SESSION['must_change_password'] = ((int)$row['must_change_password']) === 1;
    $_SESSION['user_channels'] = array_key_exists('channels', $row) ? $row['channels'] : null;
    $_SESSION['user_cache_at'] = time();
}

function auth_login_share(array $row): void {
    auth_session_start();
    session_regenerate_id(true);
    unset(
        $_SESSION['user_id'],
        $_SESSION['role'],
        $_SESSION['username'],
        $_SESSION['must_change_password'],
        $_SESSION['user_channels'],
        $_SESSION['user_cache_at']
    );
    $_SESSION['share_id'] = $row['id'];
    $channels = json_decode((string)$row['channels'], true);
    $_SESSION['share_channels'] = is_array($channels) ? $channels : [];
    $_SESSION['share_expires_at'] = $row['expires_at'];
    $_SESSION['share_name'] = $row['name'];
    $_SESSION['share_cache_at'] = time();
}

function auth_role_at_least(?array $user, array $allowed): bool {
    if ($user === null) {
        return false;
    }
    return in_array($user['role'], $allowed, true);
}

function auth_allowed_channels_for_session(): ?array {
    $user = auth_current_user();
    if ($user !== null) {
        return auth_expand_channel_paths(auth_user_channel_bases($user));
    }
    $share = auth_current_share();
    if ($share !== null) {
        $channels = json_decode((string)$share['channels'], true);
        if (!is_array($channels)) {
            return [];
        }
        return auth_expand_channel_paths($channels);
    }
    return null;
}

function auth_me_payload(): ?array {
    $user = auth_current_user();
    if ($user !== null) {
        $pub = auth_user_row_public($user);
        $pub['auth'] = 'user';
        // Always emit explicit base list for the gate (null ACL → all ch0–ch7).
        $pub['channels'] = auth_user_channel_bases($user);
        return $pub;
    }
    $share = auth_current_share();
    if ($share !== null) {
        $channels = json_decode((string)$share['channels'], true);
        if (!is_array($channels)) {
            $channels = [];
        }
        return [
            'auth' => 'share',
            'role' => 'viewer',
            'share_id' => $share['id'],
            'name' => $share['name'],
            // Force a JSON array (not object) so channelAllowed can iterate.
            'channels' => array_values(array_filter(
                $channels,
                static fn($c): bool => is_string($c) && $c !== ''
            )),
            'expires_at' => $share['expires_at'],
            'must_change_password' => false,
        ];
    }
    return null;
}

function auth_bypass_enabled(): bool {
    return getenv('NEXVUE_AUTH_BYPASS') === '1';
}

/**
 * Require an authenticated user with one of the roles.
 * @param list<string> $roles
 */
function auth_require_roles(array $roles): array {
    if (auth_bypass_enabled()) {
        return [
            'id' => 'bypass',
            'username' => 'bypass',
            'role' => $roles[0] ?? 'admin',
            'password_hash' => '',
            'email' => null,
            'must_change_password' => 0,
            'disabled_at' => null,
            'created_at' => '',
            'updated_at' => '',
            'synced_at' => null,
        ];
    }
    $user = auth_current_user();
    if ($user === null) {
        throw new RuntimeException('unauthorized');
    }
    if (!auth_role_at_least($user, $roles)) {
        throw new RuntimeException('forbidden');
    }
    return $user;
}

/** Any logged-in user or valid share session. */
function auth_require_any(): array {
    if (auth_bypass_enabled()) {
        return [
            'auth' => 'user',
            'role' => 'admin',
            'username' => 'bypass',
            'channels' => array_values(array_filter(
                auth_all_channel_paths(),
                static fn(string $p): bool => !str_ends_with($p, 'lo')
            )),
            'must_change_password' => false,
        ];
    }
    $me = auth_me_payload();
    if ($me === null) {
        throw new RuntimeException('unauthorized');
    }
    return $me;
}

/**
 * True when Authorization: Bearer <NEXVUE_API_KEY|NEXVUE_SYNC_KEY> matches,
 * or X-NexVUE-Key: <key> (for clients that cannot set Authorization).
 */
function auth_bearer_api_ok(): bool {
    $key = auth_read_api_key();
    if ($key === '') {
        return false;
    }
    $hdr = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if (is_string($hdr) && preg_match('/^Bearer\s+(.+)$/i', $hdr, $m)) {
        if (hash_equals($key, trim($m[1]))) {
            return true;
        }
    }
    $x = $_SERVER['HTTP_X_NEXVUE_KEY'] ?? '';
    if (is_string($x) && $x !== '' && hash_equals($key, trim($x))) {
        return true;
    }
    return false;
}

/** @deprecated use auth_bearer_api_ok() */
function auth_bearer_sync_ok(): bool {
    return auth_bearer_api_ok();
}

function auth_users_export(?string $since): array {
    $db = auth_db();
    if ($since !== null && $since !== '') {
        $st = $db->prepare('SELECT * FROM users WHERE updated_at > :s ORDER BY updated_at ASC');
        $st->bindValue(':s', $since, SQLITE3_TEXT);
        $r = $st->execute();
    } else {
        $r = $db->query('SELECT * FROM users ORDER BY updated_at ASC');
    }
    $out = [];
    while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
        $item = auth_user_row_public($row);
        // Sync key callers get password hashes for push/pull fidelity.
        $item['password_hash'] = $row['password_hash'];
        $out[] = $item;
    }
    return $out;
}

function auth_users_import(array $users): array {
    $upserted = 0;
    foreach ($users as $u) {
        if (!is_array($u) || empty($u['id']) || empty($u['username'])) {
            continue;
        }
        $existing = auth_user_find_by_id((string)$u['id']);
        $payload = [
            'id' => (string)$u['id'],
            'username' => (string)$u['username'],
            'email' => $u['email'] ?? null,
            'role' => (string)($u['role'] ?? 'viewer'),
            'must_change_password' => !empty($u['must_change_password']),
            'disabled_at' => $u['disabled_at'] ?? null,
            'created_at' => $u['created_at'] ?? auth_now_iso(),
            'updated_at' => $u['updated_at'] ?? auth_now_iso(),
            'synced_at' => auth_now_iso(),
        ];
        if (array_key_exists('channels', $u)) {
            $payload['channels'] = $u['channels'];
        }
        if (!empty($u['password_hash'])) {
            $payload['password_hash'] = (string)$u['password_hash'];
            $payload['password'] = 'imported-placeholder-xxxxxxxx'; // bypassed when hash set
        } elseif (!empty($u['password'])) {
            $payload['password'] = (string)$u['password'];
        } elseif ($existing === null) {
            $payload['password'] = bin2hex(random_bytes(16));
            $payload['must_change_password'] = true;
        }
        if ($existing === null) {
            if (empty($payload['password_hash']) && empty($payload['password'])) {
                continue;
            }
            // auth_user_create prefers password_hash when set.
            if (!empty($u['password_hash'])) {
                $payload['password'] = 'imported-placeholder-xxxxxxxx';
                $payload['password_hash'] = (string)$u['password_hash'];
            }
            auth_user_create($payload);
        } else {
            auth_user_update((string)$u['id'], $payload);
        }
        $upserted++;
    }
    return ['upserted' => $upserted];
}

function auth_shares_export(?string $since): array {
    $db = auth_db();
    if ($since !== null && $since !== '') {
        $st = $db->prepare('SELECT * FROM share_links WHERE updated_at > :s ORDER BY updated_at ASC');
        $st->bindValue(':s', $since, SQLITE3_TEXT);
        $r = $st->execute();
    } else {
        $r = $db->query('SELECT * FROM share_links ORDER BY updated_at ASC');
    }
    $out = [];
    while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
        $item = auth_share_row_public($row);
        $item['token_hash'] = $row['token_hash'];
        if (!empty($row['token'])) {
            $item['token'] = (string)$row['token'];
        }
        $out[] = $item;
    }
    return $out;
}

function auth_shares_import(array $shares): array {
    $upserted = 0;
    $db = auth_db();
    foreach ($shares as $s) {
        if (!is_array($s) || empty($s['id']) || empty($s['name'])) {
            continue;
        }
        $id = (string)$s['id'];
        $channels = $s['channels'] ?? [];
        if (!is_array($channels)) {
            continue;
        }
        try {
            $channels = auth_normalize_channels($channels);
        } catch (Throwable $e) {
            continue;
        }
        $expires = (string)($s['expires_at'] ?? '');
        if ($expires === '' || strtotime($expires) === false) {
            continue;
        }
        $tokenHash = (string)($s['token_hash'] ?? '');
        $rawToken = isset($s['token']) ? (string)$s['token'] : '';
        if ($tokenHash === '' && $rawToken !== '') {
            $tokenHash = auth_hash_token($rawToken);
        }
        if ($tokenHash === '') {
            continue;
        }
        $pageKey = auth_share_page_key(isset($s['page']) ? (string)$s['page'] : 'player');
        $now = auth_now_iso();
        $existing = auth_share_find_by_id($id);
        if ($existing === null) {
            $st = $db->prepare(
                'INSERT INTO share_links (id, name, token_hash, token, page, channels, expires_at, revoked_at, created_by, created_at, updated_at, synced_at)
                 VALUES (:id, :n, :th, :tok, :pg, :ch, :ex, :rv, :cb, :c, :up, :sy)'
            );
            $st->bindValue(':id', $id, SQLITE3_TEXT);
            $st->bindValue(':n', trim((string)$s['name']), SQLITE3_TEXT);
            $st->bindValue(':th', $tokenHash, SQLITE3_TEXT);
            $st->bindValue(':tok', $rawToken !== '' ? $rawToken : null, $rawToken !== '' ? SQLITE3_TEXT : SQLITE3_NULL);
            $st->bindValue(':pg', $pageKey, SQLITE3_TEXT);
            $st->bindValue(':ch', json_encode($channels, JSON_UNESCAPED_SLASHES), SQLITE3_TEXT);
            $st->bindValue(':ex', $expires, SQLITE3_TEXT);
            $st->bindValue(':rv', $s['revoked_at'] ?? null, !empty($s['revoked_at']) ? SQLITE3_TEXT : SQLITE3_NULL);
            $st->bindValue(':cb', $s['created_by'] ?? null, !empty($s['created_by']) ? SQLITE3_TEXT : SQLITE3_NULL);
            $st->bindValue(':c', $s['created_at'] ?? $now, SQLITE3_TEXT);
            $st->bindValue(':up', $s['updated_at'] ?? $now, SQLITE3_TEXT);
            $st->bindValue(':sy', $now, SQLITE3_TEXT);
            $st->execute();
        } else {
            $st = $db->prepare(
                'UPDATE share_links SET name=:n, token_hash=:th, token=COALESCE(:tok, token), page=:pg, channels=:ch, expires_at=:ex,
                 revoked_at=:rv, updated_at=:up, synced_at=:sy WHERE id=:id'
            );
            $st->bindValue(':n', trim((string)$s['name']), SQLITE3_TEXT);
            $st->bindValue(':th', $tokenHash, SQLITE3_TEXT);
            $st->bindValue(':tok', $rawToken !== '' ? $rawToken : null, $rawToken !== '' ? SQLITE3_TEXT : SQLITE3_NULL);
            $st->bindValue(':pg', $pageKey, SQLITE3_TEXT);
            $st->bindValue(':ch', json_encode($channels, JSON_UNESCAPED_SLASHES), SQLITE3_TEXT);
            $st->bindValue(':ex', $expires, SQLITE3_TEXT);
            $st->bindValue(':rv', $s['revoked_at'] ?? null, !empty($s['revoked_at']) ? SQLITE3_TEXT : SQLITE3_NULL);
            $st->bindValue(':up', $s['updated_at'] ?? $now, SQLITE3_TEXT);
            $st->bindValue(':sy', $now, SQLITE3_TEXT);
            $st->bindValue(':id', $id, SQLITE3_TEXT);
            $st->execute();
        }
        $upserted++;
    }
    return ['upserted' => $upserted];
}

function auth_try_mail_reset(string $email, string $resetUrl): bool {
    $from = getenv('NEXVUE_MAIL_FROM');
    if (!is_string($from) || $from === '') {
        $from = 'nexvue@localhost';
    }
    $subject = 'NexVUE password reset';
    $body = "A password reset was requested for your NexVUE account.\n\n"
        . "Open this link (expires in 1 hour):\n{$resetUrl}\n\n"
        . "If you did not request this, ignore this message.\n";
    $headers = 'From: ' . $from . "\r\n" . 'Content-Type: text/plain; charset=UTF-8';
    try {
        return @mail($email, $subject, $body, $headers);
    } catch (Throwable $e) {
        return false;
    }
}

function auth_try_mail_share(string $email, string $shareUrl, string $shareName, string $expiresAt): bool {
    $from = getenv('NEXVUE_MAIL_FROM');
    if (!is_string($from) || $from === '') {
        $from = 'nexvue@localhost';
    }
    $subject = 'NexVUE share link: ' . $shareName;
    $body = "You have been sent a NexVUE share link.\n\n"
        . "Name: {$shareName}\n"
        . "Expires: {$expiresAt}\n\n"
        . "Open:\n{$shareUrl}\n\n"
        . "This link can be revoked by the sender at any time.\n";
    $headers = 'From: ' . $from . "\r\n" . 'Content-Type: text/plain; charset=UTF-8';
    try {
        return @mail($email, $subject, $body, $headers);
    } catch (Throwable $e) {
        return false;
    }
}
