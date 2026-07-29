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
 * Roles: admin | operator | viewer
 * Share links: named, channel-scoped, mandatory expires_at, revocable.
 */

declare(strict_types=1);

const NEXVUE_AUTH_ROLES = ['admin', 'operator', 'viewer'];
const NEXVUE_AUTH_JWT_TTL_S = 90;
const NEXVUE_AUTH_PUBLISH_TTL_S = 315360000; // ~10 years
const NEXVUE_AUTH_RESET_TTL_S = 3600;
const NEXVUE_AUTH_MAX_CHANNELS = 8; // ch0..ch7 (+ lo)

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
    $db = auth_db();
    $db->exec(<<<'SQL'
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  email TEXT,
  role TEXT NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 0,
  disabled_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  synced_at TEXT
);
CREATE TABLE IF NOT EXISTS share_links (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
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
}

function auth_ensure_keys(): array {
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
    // Rewrite JWKS when missing/stale; tolerate a read-only tree only if a
    // usable jwks.json is already present (e.g. root-owned after bootstrap).
    $needWrite = !is_file($jwksPath)
        || trim((string)file_get_contents($jwksPath)) !== $jwksJson;
    if ($needWrite && file_put_contents($jwksPath, $jwksJson) === false) {
        if (!is_file($jwksPath)) {
            throw new RuntimeException('cannot write jwks.json');
        }
    }

    return [
        'private_pem' => $privPem,
        'public_pem' => $pubPem,
        'kid' => $kid,
        'jwks' => $jwks,
        'jwks_path' => $jwksPath,
    ];
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

function auth_read_sync_key(): string {
    $env = getenv('NEXVUE_SYNC_KEY');
    if (is_string($env) && $env !== '') {
        return $env;
    }
    $path = auth_station_env_path();
    if (!is_readable($path)) {
        return '';
    }
    $raw = (string)file_get_contents($path);
    if (preg_match('/^\s*NEXVUE_SYNC_KEY=(.*)$/m', $raw, $m)) {
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

function auth_normalize_channels(array $channels): array {
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
    sort($list, SORT_NATURAL);
    if ($list === []) {
        throw new InvalidArgumentException('at least one channel required');
    }
    return $list;
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
    return [
        'id' => $row['id'],
        'username' => $row['username'],
        'email' => $row['email'] !== null && $row['email'] !== '' ? $row['email'] : null,
        'role' => $row['role'],
        'must_change_password' => ((int)$row['must_change_password']) === 1,
        'disabled_at' => $row['disabled_at'] ?: null,
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
    $db = auth_db();
    $st = $db->prepare(
        'INSERT INTO users (id, username, password_hash, email, role, must_change_password, disabled_at, created_at, updated_at, synced_at)
         VALUES (:id, :u, :ph, :e, :r, :m, :d, :c, :up, :sy)'
    );
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->bindValue(':u', $username, SQLITE3_TEXT);
    $st->bindValue(':ph', $hash, SQLITE3_TEXT);
    $st->bindValue(':e', $email, $email === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':r', $role, SQLITE3_TEXT);
    $st->bindValue(':m', $must, SQLITE3_INTEGER);
    $st->bindValue(':d', $in['disabled_at'] ?? null, isset($in['disabled_at']) && $in['disabled_at'] ? SQLITE3_TEXT : SQLITE3_NULL);
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
    $now = auth_now_iso();
    $db = auth_db();
    $st = $db->prepare(
        'UPDATE users SET username=:u, password_hash=:ph, email=:e, role=:r,
         must_change_password=:m, disabled_at=:d, updated_at=:up, synced_at=:sy WHERE id=:id'
    );
    $st->bindValue(':u', $username, SQLITE3_TEXT);
    $st->bindValue(':ph', $hash, SQLITE3_TEXT);
    $st->bindValue(':e', $email, $email === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':r', $role, SQLITE3_TEXT);
    $st->bindValue(':m', $must, SQLITE3_INTEGER);
    $st->bindValue(':d', $disabled, $disabled === null ? SQLITE3_NULL : SQLITE3_TEXT);
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
    $out = [
        'id' => $row['id'],
        'name' => $row['name'],
        'channels' => $channels,
        'expires_at' => $row['expires_at'],
        'revoked_at' => $row['revoked_at'] ?: null,
        'status' => $status,
        'created_by' => $row['created_by'] ?: null,
        'created_at' => $row['created_at'],
        'updated_at' => $row['updated_at'],
        'synced_at' => $row['synced_at'] ?: null,
    ];
    if ($includeToken && $rawToken !== null) {
        $out['token'] = $rawToken;
    }
    return $out;
}

function auth_share_create(string $name, array $channels, string $expiresAt, ?string $createdBy): array {
    $name = trim($name);
    if ($name === '' || strlen($name) > 128) {
        throw new InvalidArgumentException('invalid share name');
    }
    $channels = auth_normalize_channels($channels);
    // Re-validate expiry is set (no open-ended).
    if ($expiresAt === '' || strtotime($expiresAt) === false) {
        throw new InvalidArgumentException('expires_at required');
    }
    if (strtotime($expiresAt) <= time()) {
        throw new InvalidArgumentException('expires_at must be in the future');
    }
    $raw = bin2hex(random_bytes(32));
    $id = auth_uuid();
    $now = auth_now_iso();
    $db = auth_db();
    $st = $db->prepare(
        'INSERT INTO share_links (id, name, token_hash, channels, expires_at, revoked_at, created_by, created_at, updated_at, synced_at)
         VALUES (:id, :n, :th, :ch, :ex, NULL, :cb, :c, :up, NULL)'
    );
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->bindValue(':n', $name, SQLITE3_TEXT);
    $st->bindValue(':th', auth_hash_token($raw), SQLITE3_TEXT);
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

function auth_shares_list(): array {
    $db = auth_db();
    $r = $db->query('SELECT * FROM share_links ORDER BY created_at DESC');
    $out = [];
    while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
        $out[] = auth_share_row_public($row);
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

function auth_current_user(): ?array {
    auth_session_start();
    $uid = $_SESSION['user_id'] ?? null;
    if (!is_string($uid) || $uid === '') {
        return null;
    }
    $row = auth_user_find_by_id($uid);
    if ($row === null || !empty($row['disabled_at'])) {
        return null;
    }
    return $row;
}

function auth_current_share(): ?array {
    auth_session_start();
    $sid = $_SESSION['share_id'] ?? null;
    if (!is_string($sid) || $sid === '') {
        return null;
    }
    $row = auth_share_find_by_id($sid);
    if (!auth_share_is_valid($row)) {
        unset($_SESSION['share_id'], $_SESSION['share_channels']);
        return null;
    }
    return $row;
}

function auth_login_user(array $row): void {
    auth_session_start();
    session_regenerate_id(true);
    unset($_SESSION['share_id'], $_SESSION['share_channels']);
    $_SESSION['user_id'] = $row['id'];
    $_SESSION['role'] = $row['role'];
}

function auth_login_share(array $row): void {
    auth_session_start();
    session_regenerate_id(true);
    unset($_SESSION['user_id'], $_SESSION['role']);
    $_SESSION['share_id'] = $row['id'];
    $channels = json_decode((string)$row['channels'], true);
    $_SESSION['share_channels'] = is_array($channels) ? $channels : [];
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
        // Local users: all station channels (share links are the scoped mechanism).
        return auth_all_channel_paths();
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
        $pub['channels'] = array_values(array_filter(
            auth_all_channel_paths(),
            static fn(string $p): bool => !str_ends_with($p, 'lo')
        ));
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
            'channels' => $channels,
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

function auth_bearer_sync_ok(): bool {
    $key = auth_read_sync_key();
    if ($key === '') {
        return false;
    }
    $hdr = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if (!is_string($hdr) || !preg_match('/^Bearer\s+(.+)$/i', $hdr, $m)) {
        return false;
    }
    return hash_equals($key, trim($m[1]));
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
        if ($tokenHash === '' && !empty($s['token'])) {
            $tokenHash = auth_hash_token((string)$s['token']);
        }
        if ($tokenHash === '') {
            continue;
        }
        $now = auth_now_iso();
        $existing = auth_share_find_by_id($id);
        if ($existing === null) {
            $st = $db->prepare(
                'INSERT INTO share_links (id, name, token_hash, channels, expires_at, revoked_at, created_by, created_at, updated_at, synced_at)
                 VALUES (:id, :n, :th, :ch, :ex, :rv, :cb, :c, :up, :sy)'
            );
            $st->bindValue(':id', $id, SQLITE3_TEXT);
            $st->bindValue(':n', trim((string)$s['name']), SQLITE3_TEXT);
            $st->bindValue(':th', $tokenHash, SQLITE3_TEXT);
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
                'UPDATE share_links SET name=:n, token_hash=:th, channels=:ch, expires_at=:ex,
                 revoked_at=:rv, updated_at=:up, synced_at=:sy WHERE id=:id'
            );
            $st->bindValue(':n', trim((string)$s['name']), SQLITE3_TEXT);
            $st->bindValue(':th', $tokenHash, SQLITE3_TEXT);
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
