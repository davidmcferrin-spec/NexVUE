<?php
/**
 * nexvue-portal-auth-lib.php — NexVUE cloud portal core (Phase 4).
 *
 * Multi-tenant from day one: every row below the org level carries an
 * org_id, and every read/write path is org-scoped by the caller's own
 * portal_users.org_id — no cross-org visibility is possible by construction
 * (see portal_user_channel_stations()/portal_catalog_list_for_user()).
 *
 * Deliberately self-contained — does NOT require_once anything from
 * web-node/. Humans authenticate via NexAPP (Alias /nexvue on the hub
 * vhost). Local bcrypt login is test-only (NEXVUE_PORTAL_TEST_AUTH=1).
 * This file has its own SQLite store and RSA signing keypair for
 * MediaMTX viewer JWTs + edge SSO JWTs. The hard wire contract with
 * every edge: portal_mint_viewer_jwt() claim shape
 * (mediamtx_permissions: [{action, path}]).
 *
 * SQLite: /var/lib/nexvue-portal/portal.db (override NEXVUE_PORTAL_DB)
 * Keys:   /var/lib/nexvue-portal/{private.pem,public.pem,jwks.json,kid}
 *         (override NEXVUE_PORTAL_DIR)
 *
 * Portal roles stay org_admin | org_operator | org_viewer internally.
 * NexAPP catalog admin maps to org_admin; catalog user maps to
 * org_viewer. NexAPP groups map to stations/channels via
 * group_station_acl; that bundle is echoed on station_heartbeat
 * (edge outbound) — the portal never calls a node.
 */

declare(strict_types=1);

const NEXVUE_PORTAL_ROLES = ['org_admin', 'org_operator', 'org_viewer'];
const NEXVUE_PORTAL_VIEWER_JWT_TTL_S = 90;
const NEXVUE_PORTAL_SCHEMA_VERSION = 4;
const NEXVUE_PORTAL_SSO_JWT_TTL_S = 180;
const NEXVUE_PORTAL_HEARTBEAT_STALE_S = 700;
const NEXVUE_PORTAL_EDGE_SYNC_ROLES = ['viewer', 'operator', 'sharer'];
const NEXVUE_PORTAL_MAX_CHANNEL_ID = 7;
/** Enrollment tokens are single-use and short-lived — an admin generates
 *  one right before pasting it into the edge's Settings → Adopt form. */
const NEXVUE_PORTAL_ENROLL_TOKEN_TTL_S = 3600;

function portal_dir(): string {
    $o = getenv('NEXVUE_PORTAL_DIR');
    if (is_string($o) && $o !== '') {
        return rtrim($o, '/\\');
    }
    return '/var/lib/nexvue-portal';
}

function portal_db_path(): string {
    $o = getenv('NEXVUE_PORTAL_DB');
    if (is_string($o) && $o !== '') {
        return $o;
    }
    return portal_dir() . '/portal.db';
}

function portal_b64url_encode(string $bin): string {
    return rtrim(strtr(base64_encode($bin), '+/', '-_'), '=');
}

function portal_uuid(): string {
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

function portal_now_iso(): string {
    return gmdate('Y-m-d\TH:i:s\Z');
}

function portal_hash_token(string $raw): string {
    return hash('sha256', $raw);
}

/** @return SQLite3 */
function portal_db(): SQLite3 {
    static $db = null;
    static $path = null;
    $p = portal_db_path();
    if ($db instanceof SQLite3 && $path === $p) {
        return $db;
    }
    $dir = dirname($p);
    if (!is_dir($dir)) {
        if (!@mkdir($dir, 0750, true) && !is_dir($dir)) {
            throw new RuntimeException('cannot create portal db dir: ' . $dir);
        }
    }
    $db = new SQLite3($p);
    $db->busyTimeout(5000);
    $db->exec('PRAGMA journal_mode=WAL');
    $db->exec('PRAGMA foreign_keys=ON');
    $path = $p;
    return $db;
}

function portal_migrate(): void {
    static $done = false;
    if ($done) {
        return;
    }
    $db = portal_db();
    $ver = (int)$db->querySingle('PRAGMA user_version');
    if ($ver >= NEXVUE_PORTAL_SCHEMA_VERSION) {
        $done = true;
        return;
    }
    if ($ver < 1) {
        $db->exec(<<<'SQL'
CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT UNIQUE COLLATE NOCASE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_users (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  email TEXT,
  role TEXT NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 0,
  disabled_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portal_users_org ON portal_users(org_id);
CREATE TABLE IF NOT EXISTS stations (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  name TEXT NOT NULL,
  edge_base_url TEXT NOT NULL,
  edge_whep_port INTEGER NOT NULL DEFAULT 8889,
  api_key_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',
  edge_version TEXT,
  last_heartbeat_at TEXT,
  last_catalog_sync_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stations_org ON stations(org_id);
CREATE TABLE IF NOT EXISTS station_channels (
  station_id TEXT NOT NULL REFERENCES stations(id),
  channel_base TEXT NOT NULL,
  alias TEXT,
  lo_enabled INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (station_id, channel_base)
);
CREATE TABLE IF NOT EXISTS catalog_acl (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL,
  portal_user_id TEXT NOT NULL REFERENCES portal_users(id),
  station_id TEXT NOT NULL REFERENCES stations(id),
  channel_base TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(portal_user_id, station_id, channel_base)
);
CREATE INDEX IF NOT EXISTS idx_catalog_acl_user ON catalog_acl(portal_user_id);
CREATE TABLE IF NOT EXISTS enrollment_tokens (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_by TEXT NOT NULL REFERENCES portal_users(id),
  expires_at TEXT NOT NULL,
  used_at TEXT,
  revoked_at TEXT,
  created_station_id TEXT REFERENCES stations(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enroll_tokens_org ON enrollment_tokens(org_id);
SQL);
        $orgCount = (int)$db->querySingle('SELECT COUNT(*) FROM orgs');
        if ($orgCount === 0) {
            $orgName = getenv('NEXVUE_PORTAL_SEED_ORG_NAME') ?: 'Default Org';
            $org = portal_org_create((string)$orgName);
            if (portal_test_auth_enabled()) {
                portal_user_create([
                    'org_id' => $org['id'],
                    'username' => 'admin',
                    'password' => 'password',
                    'role' => 'org_admin',
                    'must_change_password' => true,
                ]);
            }
        }
        $ver = 1;
    }
    if ($ver < 2) {
        $cols = [];
        $info = $db->query('PRAGMA table_info(stations)');
        if ($info) {
            while ($c = $info->fetchArray(SQLITE3_ASSOC)) {
                $cols[(string)$c['name']] = true;
            }
        }
        if (!isset($cols['ice_servers_json'])) {
            $db->exec('ALTER TABLE stations ADD COLUMN ice_servers_json TEXT');
        }
        if (!isset($cols['ice_servers_expires_at'])) {
            $db->exec('ALTER TABLE stations ADD COLUMN ice_servers_expires_at TEXT');
        }
        $ver = 2;
    }
    if ($ver < 3) {
        $cols = [];
        $info = $db->query('PRAGMA table_info(stations)');
        if ($info) {
            while ($c = $info->fetchArray(SQLITE3_ASSOC)) {
                $cols[(string)$c['name']] = true;
            }
        }
        if (!isset($cols['sfu_mode'])) {
            $db->exec("ALTER TABLE stations ADD COLUMN sfu_mode TEXT NOT NULL DEFAULT 'off'");
        }
        if (!isset($cols['sfu_play_json'])) {
            $db->exec('ALTER TABLE stations ADD COLUMN sfu_play_json TEXT');
        }
        $ver = 3;
    }
    if ($ver < 4) {
        $cols = [];
        $info = $db->query('PRAGMA table_info(portal_users)');
        if ($info) {
            while ($c = $info->fetchArray(SQLITE3_ASSOC)) {
                $cols[(string)$c['name']] = true;
            }
        }
        if (!isset($cols['identity_key'])) {
            $db->exec('ALTER TABLE portal_users ADD COLUMN identity_key TEXT');
        }
        if (!isset($cols['catalog_role'])) {
            $db->exec("ALTER TABLE portal_users ADD COLUMN catalog_role TEXT NOT NULL DEFAULT 'user'");
        }
        $db->exec('CREATE UNIQUE INDEX IF NOT EXISTS idx_portal_users_identity ON portal_users(identity_key)');
        $db->exec(<<<'SQL'
CREATE TABLE IF NOT EXISTS group_station_acl (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL,
  nexapp_group_id TEXT NOT NULL,
  nexapp_group_name TEXT,
  station_id TEXT NOT NULL REFERENCES stations(id),
  channel_base TEXT,
  edge_role TEXT NOT NULL DEFAULT 'viewer',
  created_at TEXT NOT NULL,
  UNIQUE(nexapp_group_id, station_id, channel_base)
);
CREATE INDEX IF NOT EXISTS idx_group_acl_station ON group_station_acl(station_id);
SQL);
        $ver = 4;
    }
    $db->exec('PRAGMA user_version = ' . (string)NEXVUE_PORTAL_SCHEMA_VERSION);
    $done = true;
}

// ---------------------------------------------------------------------------
// Signing keypair (mirrors web-node/nexvue-auth-lib.php's auth_ensure_keys())
// ---------------------------------------------------------------------------

function portal_ensure_keys(): array {
    static $cached = null;
    if (is_array($cached)) {
        return $cached;
    }
    $dir = portal_dir();
    if (!is_dir($dir)) {
        if (!@mkdir($dir, 0750, true) && !is_dir($dir)) {
            throw new RuntimeException('cannot create portal dir: ' . $dir);
        }
    }
    $privPath = $dir . '/private.pem';
    $pubPath = $dir . '/public.pem';
    $kidPath = $dir . '/kid';
    $jwksPath = $dir . '/jwks.json';

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
                $cached = ['private_pem' => $privPem, 'public_pem' => $pubPem, 'kid' => $kid, 'jwks' => $jwks];
                return $cached;
            }
        }
    }

    if (!is_file($privPath) || !is_file($pubPath)) {
        $res = openssl_pkey_new(['private_key_type' => OPENSSL_KEYTYPE_RSA, 'private_key_bits' => 2048]);
        if ($res === false) {
            throw new RuntimeException('openssl_pkey_new failed');
        }
        openssl_pkey_export($res, $privPem);
        $details = openssl_pkey_get_details($res);
        if ($details === false || empty($details['key'])) {
            throw new RuntimeException('openssl_pkey_get_details failed');
        }
        $pubPem = $details['key'];
        if (file_put_contents($privPath, $privPem) === false || file_put_contents($pubPath, $pubPem) === false) {
            throw new RuntimeException('cannot write portal keypair');
        }
        @chmod($privPath, 0640);
        @chmod($pubPath, 0644);
        $kid = portal_b64url_encode(random_bytes(8));
        file_put_contents($kidPath, $kid);
    }

    $kid = is_file($kidPath) ? trim((string)file_get_contents($kidPath)) : '';
    if ($kid === '') {
        $kid = portal_b64url_encode(random_bytes(8));
        file_put_contents($kidPath, $kid);
    }
    $privPem = (string)file_get_contents($privPath);
    $pubPem = (string)file_get_contents($pubPath);
    if ($privPem === '' || $pubPem === '') {
        throw new RuntimeException('portal keypair unreadable');
    }
    $jwks = portal_build_jwks($pubPem, $kid);
    $jwksJson = json_encode($jwks, JSON_UNESCAPED_SLASHES);
    if ($jwksJson === false) {
        throw new RuntimeException('jwks encode failed');
    }
    if ((!is_file($jwksPath) || trim((string)file_get_contents($jwksPath)) !== $jwksJson)
        && file_put_contents($jwksPath, $jwksJson) === false
        && !is_file($jwksPath)) {
        throw new RuntimeException('cannot write jwks.json');
    }
    $cached = ['private_pem' => $privPem, 'public_pem' => $pubPem, 'kid' => $kid, 'jwks' => $jwks];
    return $cached;
}

function portal_build_jwks(string $pubPem, string $kid): array {
    $res = openssl_pkey_get_public($pubPem);
    if ($res === false) {
        throw new RuntimeException('invalid public key');
    }
    $details = openssl_pkey_get_details($res);
    if ($details === false || ($details['type'] ?? null) !== OPENSSL_KEYTYPE_RSA) {
        throw new RuntimeException('expected RSA public key');
    }
    return [
        'keys' => [[
            'kty' => 'RSA', 'use' => 'sig', 'alg' => 'RS256', 'kid' => $kid,
            'n' => portal_b64url_encode($details['rsa']['n']),
            'e' => portal_b64url_encode($details['rsa']['e']),
        ]],
    ];
}

function portal_jwt_encode(array $claims, ?int $ttlS = null): string {
    $keys = portal_ensure_keys();
    $now = time();
    $ttl = $ttlS ?? NEXVUE_PORTAL_VIEWER_JWT_TTL_S;
    $payload = array_merge(['iat' => $now, 'exp' => $now + $ttl, 'iss' => 'nexvue-portal'], $claims);
    $header = ['alg' => 'RS256', 'typ' => 'JWT', 'kid' => $keys['kid']];
    $h = portal_b64url_encode(json_encode($header, JSON_UNESCAPED_SLASHES));
    $p = portal_b64url_encode(json_encode($payload, JSON_UNESCAPED_SLASHES));
    $data = $h . '.' . $p;
    $sig = '';
    if (!openssl_sign($data, $sig, $keys['private_pem'], OPENSSL_ALGO_SHA256)) {
        throw new RuntimeException('openssl_sign failed');
    }
    return $data . '.' . portal_b64url_encode($sig);
}

/**
 * Mint a viewer JWT for one channel on one station. Claim shape is a hard
 * wire contract with MediaMTX (authJWTClaimKey: mediamtx_permissions on
 * every edge) — do not change this shape without also changing every
 * edge's mediamtx.yml.
 */
function portal_mint_viewer_jwt(string $sub, string $channelBase): string {
    if (!preg_match('/^ch[0-7]$/', $channelBase)) {
        throw new InvalidArgumentException('invalid channel: ' . $channelBase);
    }
    return portal_jwt_encode([
        'sub' => $sub,
        'mediamtx_permissions' => [
            ['action' => 'read', 'path' => $channelBase],
            ['action' => 'read', 'path' => $channelBase . 'lo'],
        ],
    ]);
}

// ---------------------------------------------------------------------------
// Orgs
// ---------------------------------------------------------------------------

function portal_org_create(string $name): array {
    $name = trim($name);
    if ($name === '' || strlen($name) > 128) {
        throw new InvalidArgumentException('invalid org name');
    }
    $id = portal_uuid();
    $now = portal_now_iso();
    $slug = strtolower((string)preg_replace('/[^a-z0-9]+/', '-', $name));
    $slug = trim($slug, '-') ?: substr($id, 0, 8);
    $db = portal_db();
    $st = $db->prepare('INSERT INTO orgs (id, name, slug, created_at, updated_at) VALUES (:id, :n, :s, :c, :u)');
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->bindValue(':n', $name, SQLITE3_TEXT);
    $st->bindValue(':s', $slug, SQLITE3_TEXT);
    $st->bindValue(':c', $now, SQLITE3_TEXT);
    $st->bindValue(':u', $now, SQLITE3_TEXT);
    if (!$st->execute()) {
        throw new RuntimeException('org create failed');
    }
    return ['id' => $id, 'name' => $name, 'slug' => $slug, 'created_at' => $now, 'updated_at' => $now];
}

function portal_org_find_by_id(string $id): ?array {
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM orgs WHERE id = :id LIMIT 1');
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

// ---------------------------------------------------------------------------
// Portal users
// ---------------------------------------------------------------------------

function portal_normalize_username(string $u): string {
    $u = trim($u);
    if ($u === '' || strlen($u) > 64 || !preg_match('/^[A-Za-z0-9._@+-]+$/', $u)) {
        throw new InvalidArgumentException('invalid username');
    }
    return $u;
}

function portal_normalize_role(string $role): string {
    $role = strtolower(trim($role));
    if (!in_array($role, NEXVUE_PORTAL_ROLES, true)) {
        throw new InvalidArgumentException('invalid role');
    }
    return $role;
}

function portal_user_row_public(array $row): array {
    return [
        'id' => $row['id'],
        'org_id' => $row['org_id'],
        'username' => $row['username'],
        'email' => $row['email'] !== null && $row['email'] !== '' ? $row['email'] : null,
        'role' => $row['role'],
        'catalog_role' => ($row['catalog_role'] ?? '') === 'admin' ? 'admin' : 'user',
        'identity_key' => $row['identity_key'] ?? null,
        'must_change_password' => ((int)$row['must_change_password']) === 1,
        'disabled_at' => $row['disabled_at'] ?: null,
        'created_at' => $row['created_at'],
        'updated_at' => $row['updated_at'],
    ];
}

function portal_user_find_by_username(string $username): ?array {
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM portal_users WHERE username = :u COLLATE NOCASE LIMIT 1');
    $st->bindValue(':u', $username, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function portal_user_find_by_id(string $id): ?array {
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM portal_users WHERE id = :id LIMIT 1');
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

/** @param array{org_id:string,username:string,password?:string,password_hash?:string,role:string,email?:?string,must_change_password?:bool} $in */
function portal_user_create(array $in): array {
    $orgId = (string)($in['org_id'] ?? '');
    if ($orgId === '' || portal_org_find_by_id($orgId) === null) {
        throw new InvalidArgumentException('invalid org_id');
    }
    $username = portal_normalize_username((string)($in['username'] ?? ''));
    $role = portal_normalize_role((string)($in['role'] ?? 'org_viewer'));
    $email = isset($in['email']) && is_string($in['email']) && $in['email'] !== '' ? trim($in['email']) : null;
    if ($email !== null && (strlen($email) > 254 || !filter_var($email, FILTER_VALIDATE_EMAIL))) {
        throw new InvalidArgumentException('invalid email');
    }
    $must = !empty($in['must_change_password']) ? 1 : 0;
    $id = portal_uuid();
    $now = portal_now_iso();
    if (isset($in['password_hash']) && is_string($in['password_hash']) && $in['password_hash'] !== '') {
        $hash = $in['password_hash'];
    } else {
        $password = (string)($in['password'] ?? '');
        if (strlen($password) < 8) {
            throw new InvalidArgumentException('password must be at least 8 characters');
        }
        $hash = password_hash($password, PASSWORD_BCRYPT);
    }
    $db = portal_db();
    $st = $db->prepare(
        'INSERT INTO portal_users (id, org_id, username, password_hash, email, role, must_change_password, created_at, updated_at)
         VALUES (:id, :org, :u, :ph, :e, :r, :m, :c, :up)'
    );
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->bindValue(':org', $orgId, SQLITE3_TEXT);
    $st->bindValue(':u', $username, SQLITE3_TEXT);
    $st->bindValue(':ph', $hash, SQLITE3_TEXT);
    $st->bindValue(':e', $email, $email === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':r', $role, SQLITE3_TEXT);
    $st->bindValue(':m', $must, SQLITE3_INTEGER);
    $st->bindValue(':c', $now, SQLITE3_TEXT);
    $st->bindValue(':up', $now, SQLITE3_TEXT);
    if (!$st->execute()) {
        throw new RuntimeException('portal user create failed (username taken?)');
    }
    $row = portal_user_find_by_id($id);
    if ($row === null) {
        throw new RuntimeException('portal user create failed');
    }
    return $row;
}

/**
 * Update a user. $actorOrgId, when provided, enforces that only a row
 * already in that org may be modified — callers must always pass the
 * acting admin's own org_id here; org_id itself can never be changed via
 * this function (no cross-org reassignment).
 */
function portal_user_update(string $id, array $in, ?string $actorOrgId = null): array {
    $row = portal_user_find_by_id($id);
    if ($row === null) {
        throw new InvalidArgumentException('user not found');
    }
    if ($actorOrgId !== null && $row['org_id'] !== $actorOrgId) {
        throw new InvalidArgumentException('user not found');
    }
    $role = array_key_exists('role', $in) ? portal_normalize_role((string)$in['role']) : $row['role'];
    $email = $row['email'];
    if (array_key_exists('email', $in)) {
        $e = $in['email'];
        $email = ($e === null || $e === '') ? null : trim((string)$e);
        if ($email !== null && (strlen($email) > 254 || !filter_var($email, FILTER_VALIDATE_EMAIL))) {
            throw new InvalidArgumentException('invalid email');
        }
    }
    $disabled = array_key_exists('disabled', $in)
        ? (!empty($in['disabled']) ? portal_now_iso() : null)
        : ($row['disabled_at'] ?: null);
    $must = array_key_exists('must_change_password', $in)
        ? (!empty($in['must_change_password']) ? 1 : 0)
        : (int)$row['must_change_password'];
    $hash = $row['password_hash'];
    if (!empty($in['password'])) {
        $pw = (string)$in['password'];
        if (strlen($pw) < 8) {
            throw new InvalidArgumentException('password must be at least 8 characters');
        }
        $hash = password_hash($pw, PASSWORD_BCRYPT);
        $must = array_key_exists('must_change_password', $in) ? $must : 0;
    }
    $now = portal_now_iso();
    $db = portal_db();
    $st = $db->prepare(
        'UPDATE portal_users SET password_hash=:ph, email=:e, role=:r, must_change_password=:m, disabled_at=:d, updated_at=:up WHERE id=:id'
    );
    $st->bindValue(':ph', $hash, SQLITE3_TEXT);
    $st->bindValue(':e', $email, $email === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':r', $role, SQLITE3_TEXT);
    $st->bindValue(':m', $must, SQLITE3_INTEGER);
    $st->bindValue(':d', $disabled, $disabled === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':up', $now, SQLITE3_TEXT);
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    if (!$st->execute()) {
        throw new RuntimeException('portal user update failed');
    }
    $out = portal_user_find_by_id($id);
    if ($out === null) {
        throw new RuntimeException('portal user update failed');
    }
    return $out;
}

function portal_user_verify(string $username, string $password): ?array {
    $row = portal_user_find_by_username($username);
    if ($row === null || !empty($row['disabled_at'])) {
        return null;
    }
    if (!password_verify($password, $row['password_hash'])) {
        return null;
    }
    return $row;
}

/** @return list<array<string,mixed>> */
function portal_users_list_for_org(string $orgId): array {
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM portal_users WHERE org_id = :org ORDER BY username COLLATE NOCASE');
    $st->bindValue(':org', $orgId, SQLITE3_TEXT);
    $r = $st->execute();
    $out = [];
    if ($r) {
        while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
            $out[] = portal_user_row_public($row);
        }
    }
    return $out;
}

// ---------------------------------------------------------------------------
// Stations
// ---------------------------------------------------------------------------

function portal_station_row_public(array $row): array {
    $hb = $row['last_heartbeat_at'] ?: null;
    return [
        'id' => $row['id'],
        'org_id' => $row['org_id'],
        'name' => $row['name'],
        'edge_base_url' => $row['edge_base_url'],
        'edge_whep_port' => (int)$row['edge_whep_port'],
        'status' => $row['status'],
        'edge_version' => $row['edge_version'] ?: null,
        'last_heartbeat_at' => $hb,
        'health' => portal_station_health_from_heartbeat($hb),
        'created_at' => $row['created_at'],
        'updated_at' => $row['updated_at'],
    ];
}

function portal_station_health_from_heartbeat(?string $lastHeartbeatAt): string {
    if ($lastHeartbeatAt === null || $lastHeartbeatAt === '') {
        return 'unknown';
    }
    $ts = strtotime($lastHeartbeatAt);
    if ($ts === false) {
        return 'unknown';
    }
    if ((time() - $ts) > NEXVUE_PORTAL_HEARTBEAT_STALE_S) {
        return 'down';
    }
    return 'ok';
}

function portal_station_find_by_id(string $id): ?array {
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM stations WHERE id = :id LIMIT 1');
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function portal_station_find_by_api_key(string $rawKey): ?array {
    $db = portal_db();
    $st = $db->prepare("SELECT * FROM stations WHERE api_key_hash = :h AND status = 'active' LIMIT 1");
    $st->bindValue(':h', portal_hash_token($rawKey), SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

/** @return list<array<string,mixed>> */
function portal_stations_list_for_org(string $orgId): array {
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM stations WHERE org_id = :org ORDER BY name COLLATE NOCASE');
    $st->bindValue(':org', $orgId, SQLITE3_TEXT);
    $r = $st->execute();
    $out = [];
    if ($r) {
        while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
            $out[] = portal_station_row_public($row);
        }
    }
    return $out;
}

function portal_station_touch_heartbeat(string $id, string $edgeVersion): void {
    $now = portal_now_iso();
    $db = portal_db();
    $st = $db->prepare(
        'UPDATE stations SET last_heartbeat_at=:h, last_catalog_sync_at=:h, edge_version=:v, updated_at=:up WHERE id=:id'
    );
    $st->bindValue(':h', $now, SQLITE3_TEXT);
    $st->bindValue(':v', $edgeVersion !== '' ? $edgeVersion : null, $edgeVersion !== '' ? SQLITE3_TEXT : SQLITE3_NULL);
    $st->bindValue(':up', $now, SQLITE3_TEXT);
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->execute();
}

/**
 * Cache short-lived Cloudflare ICE servers pushed by the edge heartbeat.
 * Empty $servers clears the cache (TURN off on the station).
 *
 * @param list<array<string,mixed>> $servers
 */
function portal_station_ice_servers_store(string $id, array $servers, string $expiresAt): void {
    $now = portal_now_iso();
    $json = null;
    $exp = null;
    if ($servers !== []) {
        $encoded = json_encode(array_values($servers), JSON_UNESCAPED_SLASHES);
        if (is_string($encoded)) {
            $json = $encoded;
            $exp = $expiresAt !== '' ? $expiresAt : null;
        }
    }
    $db = portal_db();
    $st = $db->prepare(
        'UPDATE stations SET ice_servers_json=:j, ice_servers_expires_at=:e, updated_at=:u WHERE id=:id'
    );
    $st->bindValue(':j', $json, $json === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':e', $exp, $exp === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':u', $now, SQLITE3_TEXT);
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->execute();
}

/**
 * Viewer ICE servers if the heartbeat cache is still valid.
 *
 * @return list<array<string,mixed>>
 */
function portal_station_ice_servers_for_viewer(?array $station): array {
    if ($station === null) {
        return [];
    }
    $exp = (string)($station['ice_servers_expires_at'] ?? '');
    if ($exp !== '') {
        $ts = strtotime($exp);
        if ($ts !== false && $ts <= time() + 60) {
            return [];
        }
    }
    $raw = (string)($station['ice_servers_json'] ?? '');
    if ($raw === '') {
        return [];
    }
    $decoded = json_decode($raw, true);
    return is_array($decoded) ? $decoded : [];
}

/** @return array<string,string> */
function portal_sfu_sanitize_play_map(mixed $raw): array {
    if (!is_array($raw)) {
        return [];
    }
    $out = [];
    foreach ($raw as $path => $url) {
        $p = strtolower(trim((string)$path));
        if (!preg_match('/^ch([0-9]|1[0-5])(lo)?$/', $p)) {
            continue;
        }
        $u = trim((string)$url);
        $scheme = parse_url($u, PHP_URL_SCHEME);
        if (!in_array($scheme, ['https', 'http'], true)) {
            continue;
        }
        $out[$p] = $u;
    }
    return $out;
}

function portal_sfu_sanitize_mode(string $raw): string {
    $v = strtolower(trim($raw));
    return in_array($v, ['off', 'hybrid', 'sfu'], true) ? $v : 'off';
}

/**
 * Cache Stream play URLs pushed by the edge heartbeat (never publish URLs).
 *
 * @param array<string,mixed> $play
 */
function portal_station_sfu_store(string $id, string $mode, array $play): void {
    $now = portal_now_iso();
    $mode = portal_sfu_sanitize_mode($mode);
    $map = portal_sfu_sanitize_play_map($play);
    $json = null;
    if ($map !== []) {
        $encoded = json_encode($map, JSON_UNESCAPED_SLASHES);
        if (is_string($encoded)) {
            $json = $encoded;
        }
    }
    $db = portal_db();
    $st = $db->prepare(
        'UPDATE stations SET sfu_mode=:m, sfu_play_json=:j, updated_at=:u WHERE id=:id'
    );
    $st->bindValue(':m', $mode, SQLITE3_TEXT);
    $st->bindValue(':j', $json, $json === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':u', $now, SQLITE3_TEXT);
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->execute();
}

function portal_station_sfu_play_url(?array $station, string $path): string {
    if ($station === null) {
        return '';
    }
    $mode = portal_sfu_sanitize_mode((string)($station['sfu_mode'] ?? 'off'));
    if ($mode === 'off') {
        return '';
    }
    $raw = (string)($station['sfu_play_json'] ?? '');
    if ($raw === '') {
        return '';
    }
    $decoded = json_decode($raw, true);
    if (!is_array($decoded)) {
        return '';
    }
    $path = strtolower(trim($path));
    return (string)($decoded[$path] ?? '');
}

/**
 * @return array{status:int,body:string}
 */
function portal_sfu_http(string $method, string $url, ?string $rawBody = null, string $contentType = 'application/sdp'): array {
    $stub = getenv('NEXVUE_PORTAL_SFU_HTTP_STUB');
    if (is_string($stub) && $stub !== '' && is_file($stub)) {
        $statusRaw = getenv('NEXVUE_PORTAL_SFU_HTTP_STATUS');
        $status = is_string($statusRaw) && ctype_digit($statusRaw) ? (int)$statusRaw : 200;
        return ['status' => $status, 'body' => (string)file_get_contents($stub)];
    }
    if (getenv('NEXVUE_PORTAL_SFU_HTTP_FAIL') === '1') {
        return ['status' => 0, 'body' => ''];
    }
    $scheme = parse_url($url, PHP_URL_SCHEME);
    if (!in_array($scheme, ['https', 'http'], true)) {
        return ['status' => 0, 'body' => ''];
    }
    $headers = 'Content-Type: ' . $contentType . "\r\n";
    $ctx = stream_context_create([
        'http' => [
            'method' => strtoupper($method),
            'header' => $headers,
            'content' => $rawBody ?? '',
            'timeout' => 12,
            'ignore_errors' => true,
        ],
        'ssl' => [
            'verify_peer' => true,
            'verify_peer_name' => true,
        ],
    ]);
    $out = @file_get_contents($url, false, $ctx);
    $status = 0;
    if (isset($http_response_header[0]) && preg_match('/\s(\d{3})\s/', $http_response_header[0], $m)) {
        $status = (int)$m[1];
    }
    return ['status' => $status, 'body' => is_string($out) ? $out : ''];
}

function portal_sfu_whep_exchange(string $playUrl, string $sdp): string {
    $sdp = trim($sdp);
    if ($sdp === '' || !str_starts_with($sdp, 'v=0')) {
        throw new InvalidArgumentException('invalid SDP offer');
    }
    $scheme = parse_url($playUrl, PHP_URL_SCHEME);
    if (!in_array($scheme, ['https', 'http'], true)) {
        throw new RuntimeException('Stream playback URL is not configured');
    }
    $r = portal_sfu_http('POST', $playUrl, $sdp, 'application/sdp');
    if ($r['status'] < 200 || $r['status'] >= 300 || trim($r['body']) === '') {
        if ($r['status'] === 0) {
            throw new RuntimeException('Could not reach Cloudflare Stream');
        }
        throw new RuntimeException('Stream WHEP failed (HTTP ' . $r['status'] . ')');
    }
    return $r['body'];
}

/**
 * @param list<array{channel_base:string,alias?:string,lo_enabled?:bool,active?:bool}> $channels
 */
function portal_station_channels_upsert(string $stationId, array $channels): void {
    $db = portal_db();
    $now = portal_now_iso();
    $seen = [];
    foreach ($channels as $c) {
        $base = is_array($c) ? (string)($c['channel_base'] ?? '') : '';
        if (!preg_match('/^ch[0-7]$/', $base)) {
            continue;
        }
        $seen[] = $base;
        $alias = is_array($c) && isset($c['alias']) ? (string)$c['alias'] : '';
        $lo = is_array($c) && !empty($c['lo_enabled']) ? 1 : 0;
        $active = is_array($c) && array_key_exists('active', $c) ? (!empty($c['active']) ? 1 : 0) : 1;
        $st = $db->prepare(
            'INSERT INTO station_channels (station_id, channel_base, alias, lo_enabled, active, updated_at)
             VALUES (:s, :c, :a, :lo, :act, :u)
             ON CONFLICT(station_id, channel_base) DO UPDATE SET
               alias=excluded.alias, lo_enabled=excluded.lo_enabled, active=excluded.active, updated_at=excluded.updated_at'
        );
        $st->bindValue(':s', $stationId, SQLITE3_TEXT);
        $st->bindValue(':c', $base, SQLITE3_TEXT);
        $st->bindValue(':a', $alias, SQLITE3_TEXT);
        $st->bindValue(':lo', $lo, SQLITE3_INTEGER);
        $st->bindValue(':act', $active, SQLITE3_INTEGER);
        $st->bindValue(':u', $now, SQLITE3_TEXT);
        $st->execute();
    }
    // Channels the edge no longer reports (e.g. slot parked) go inactive —
    // never deleted, so any existing catalog_acl grants aren't silently lost.
    if ($seen !== []) {
        $placeholders = implode(',', array_fill(0, count($seen), '?'));
        $st = $db->prepare(
            "UPDATE station_channels SET active=0, updated_at=? WHERE station_id=? AND channel_base NOT IN ({$placeholders})"
        );
        $i = 1;
        $st->bindValue($i++, $now, SQLITE3_TEXT);
        $st->bindValue($i++, $stationId, SQLITE3_TEXT);
        foreach ($seen as $base) {
            $st->bindValue($i++, $base, SQLITE3_TEXT);
        }
        $st->execute();
    }
}

/** @return list<array{channel_base:string, alias:string, lo_enabled:bool}> */
function portal_station_channels_list(string $stationId, bool $activeOnly = true): array {
    $db = portal_db();
    $sql = 'SELECT * FROM station_channels WHERE station_id = :s';
    if ($activeOnly) {
        $sql .= ' AND active = 1';
    }
    $sql .= ' ORDER BY channel_base';
    $st = $db->prepare($sql);
    $st->bindValue(':s', $stationId, SQLITE3_TEXT);
    $r = $st->execute();
    $out = [];
    if ($r) {
        while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
            $out[] = [
                'channel_base' => $row['channel_base'],
                'alias' => $row['alias'] ?: '',
                'lo_enabled' => ((int)$row['lo_enabled']) === 1,
            ];
        }
    }
    return $out;
}

// ---------------------------------------------------------------------------
// Enrollment
// ---------------------------------------------------------------------------

/** @return array{row: array, token: string} */
function portal_enroll_token_create(string $orgId, string $name, string $createdByUserId): array {
    $name = trim($name);
    if ($name === '' || strlen($name) > 128) {
        throw new InvalidArgumentException('invalid station name');
    }
    $raw = bin2hex(random_bytes(24));
    $id = portal_uuid();
    $now = portal_now_iso();
    $exp = gmdate('Y-m-d\TH:i:s\Z', time() + NEXVUE_PORTAL_ENROLL_TOKEN_TTL_S);
    $db = portal_db();
    $st = $db->prepare(
        'INSERT INTO enrollment_tokens (id, org_id, name, token_hash, created_by, expires_at, created_at)
         VALUES (:id, :org, :n, :th, :cb, :ex, :c)'
    );
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->bindValue(':org', $orgId, SQLITE3_TEXT);
    $st->bindValue(':n', $name, SQLITE3_TEXT);
    $st->bindValue(':th', portal_hash_token($raw), SQLITE3_TEXT);
    $st->bindValue(':cb', $createdByUserId, SQLITE3_TEXT);
    $st->bindValue(':ex', $exp, SQLITE3_TEXT);
    $st->bindValue(':c', $now, SQLITE3_TEXT);
    if (!$st->execute()) {
        throw new RuntimeException('enrollment token create failed');
    }
    return ['row' => portal_enroll_token_find_by_id($id), 'token' => $raw];
}

function portal_enroll_token_find_by_id(string $id): ?array {
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM enrollment_tokens WHERE id = :id LIMIT 1');
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function portal_enroll_token_find_by_raw(string $raw): ?array {
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM enrollment_tokens WHERE token_hash = :h LIMIT 1');
    $st->bindValue(':h', portal_hash_token($raw), SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function portal_enroll_token_revoke(string $id, string $orgId): void {
    $db = portal_db();
    $st = $db->prepare('UPDATE enrollment_tokens SET revoked_at=:r WHERE id=:id AND org_id=:org');
    $st->bindValue(':r', portal_now_iso(), SQLITE3_TEXT);
    $st->bindValue(':id', $id, SQLITE3_TEXT);
    $st->bindValue(':org', $orgId, SQLITE3_TEXT);
    $st->execute();
}

/**
 * Consume an enrollment token: creates the station on first use, or — for
 * a resubmit of the same token after a successful first exchange (e.g. the
 * edge's HTTP response was lost after our write already committed) —
 * returns the SAME station + a freshly minted API key rather than failing.
 * This is deliberately minimal idempotency, not a full exactly-once
 * protocol: a resubmit rotates the station's api_key_hash, which is safe
 * (the edge only ever needs its most recent key) but does mean a stale
 * key value from an earlier successful-but-unacknowledged exchange stops
 * working — acceptable since the edge always uses whatever came back from
 * its own most recent call.
 *
 * @return array{station: array, api_key: string}
 * @throws InvalidArgumentException on invalid/expired/revoked token
 */
function portal_enroll_token_consume(string $raw, string $edgeBaseUrl, string $edgeVersion): array {
    $row = portal_enroll_token_find_by_raw($raw);
    if ($row === null) {
        throw new InvalidArgumentException('invalid enrollment token');
    }
    if (!empty($row['revoked_at'])) {
        throw new InvalidArgumentException('enrollment token revoked');
    }
    $exp = strtotime((string)$row['expires_at']);
    if ($exp === false || $exp <= time()) {
        throw new InvalidArgumentException('enrollment token expired');
    }
    $apiKey = bin2hex(random_bytes(32));
    $now = portal_now_iso();
    $db = portal_db();

    if (!empty($row['used_at']) && !empty($row['created_station_id'])) {
        // Idempotent resubmit — same station, rotated key.
        $station = portal_station_find_by_id((string)$row['created_station_id']);
        if ($station === null) {
            throw new RuntimeException('enrollment token points at a missing station');
        }
        $st = $db->prepare('UPDATE stations SET api_key_hash=:h, edge_base_url=:u, edge_version=:v, updated_at=:up WHERE id=:id');
        $st->bindValue(':h', portal_hash_token($apiKey), SQLITE3_TEXT);
        $st->bindValue(':u', $edgeBaseUrl, SQLITE3_TEXT);
        $st->bindValue(':v', $edgeVersion !== '' ? $edgeVersion : null, $edgeVersion !== '' ? SQLITE3_TEXT : SQLITE3_NULL);
        $st->bindValue(':up', $now, SQLITE3_TEXT);
        $st->bindValue(':id', $station['id'], SQLITE3_TEXT);
        $st->execute();
        $station = portal_station_find_by_id($station['id']);
        return ['station' => $station, 'api_key' => $apiKey];
    }
    if (!empty($row['used_at'])) {
        throw new InvalidArgumentException('enrollment token already used');
    }

    $stationId = portal_uuid();
    $st = $db->prepare(
        'INSERT INTO stations (id, org_id, name, edge_base_url, api_key_hash, edge_version, created_at, updated_at)
         VALUES (:id, :org, :n, :u, :h, :v, :c, :up)'
    );
    $st->bindValue(':id', $stationId, SQLITE3_TEXT);
    $st->bindValue(':org', $row['org_id'], SQLITE3_TEXT);
    $st->bindValue(':n', $row['name'], SQLITE3_TEXT);
    $st->bindValue(':u', $edgeBaseUrl, SQLITE3_TEXT);
    $st->bindValue(':h', portal_hash_token($apiKey), SQLITE3_TEXT);
    $st->bindValue(':v', $edgeVersion !== '' ? $edgeVersion : null, $edgeVersion !== '' ? SQLITE3_TEXT : SQLITE3_NULL);
    $st->bindValue(':c', $now, SQLITE3_TEXT);
    $st->bindValue(':up', $now, SQLITE3_TEXT);
    if (!$st->execute()) {
        throw new RuntimeException('station create failed');
    }
    $mark = $db->prepare('UPDATE enrollment_tokens SET used_at=:u, created_station_id=:sid WHERE id=:id');
    $mark->bindValue(':u', $now, SQLITE3_TEXT);
    $mark->bindValue(':sid', $stationId, SQLITE3_TEXT);
    $mark->bindValue(':id', $row['id'], SQLITE3_TEXT);
    $mark->execute();

    $station = portal_station_find_by_id($stationId);
    if ($station === null) {
        throw new RuntimeException('station create failed');
    }
    return ['station' => $station, 'api_key' => $apiKey];
}

// ---------------------------------------------------------------------------
// Catalog ACL
// ---------------------------------------------------------------------------

/**
 * Replace a viewer's grants for one station with exactly $channels (empty
 * array revokes all grants on that station; null-equivalent "all channels"
 * is expressed as a single row with channel_base = NULL).
 *
 * @param list<string>|null $channels null = grant all channels on the station
 */
function portal_catalog_acl_put(string $orgId, string $portalUserId, string $stationId, ?array $channels): void {
    $user = portal_user_find_by_id($portalUserId);
    if ($user === null || $user['org_id'] !== $orgId) {
        throw new InvalidArgumentException('user not found in org');
    }
    $station = portal_station_find_by_id($stationId);
    if ($station === null || $station['org_id'] !== $orgId) {
        throw new InvalidArgumentException('station not found in org');
    }
    $db = portal_db();
    $del = $db->prepare('DELETE FROM catalog_acl WHERE portal_user_id = :u AND station_id = :s');
    $del->bindValue(':u', $portalUserId, SQLITE3_TEXT);
    $del->bindValue(':s', $stationId, SQLITE3_TEXT);
    $del->execute();

    $rows = $channels === null ? [null] : array_values(array_unique($channels));
    foreach ($rows as $base) {
        if ($base !== null && !preg_match('/^ch[0-7]$/', (string)$base)) {
            throw new InvalidArgumentException('invalid channel: ' . $base);
        }
        $st = $db->prepare(
            'INSERT INTO catalog_acl (id, org_id, portal_user_id, station_id, channel_base, created_at)
             VALUES (:id, :org, :u, :s, :c, :cr)'
        );
        $st->bindValue(':id', portal_uuid(), SQLITE3_TEXT);
        $st->bindValue(':org', $orgId, SQLITE3_TEXT);
        $st->bindValue(':u', $portalUserId, SQLITE3_TEXT);
        $st->bindValue(':s', $stationId, SQLITE3_TEXT);
        $st->bindValue(':c', $base, $base === null ? SQLITE3_NULL : SQLITE3_TEXT);
        $st->bindValue(':cr', portal_now_iso(), SQLITE3_TEXT);
        $st->execute();
    }
}

/**
 * Catalog visible to one portal user: org_admin/org_operator see every
 * active station+channel in their org; org_viewer sees only what
 * catalog_acl grants. Always org-scoped by the user's own org_id.
 *
 * @return list<array{id:string,name:string,status:string,channels:list<array>}>
 */
function portal_catalog_list_for_user(array $user): array {
    $stations = portal_stations_list_for_org($user['org_id']);
    $seeAll = in_array($user['role'], ['org_admin', 'org_operator'], true);
    $grants = $seeAll ? null : portal_user_channel_grants_map($user);
    $out = [];
    foreach ($stations as $station) {
        if ($station['status'] !== 'active') {
            continue;
        }
        $allowedChannels = null;
        if (!$seeAll) {
            if ($grants === null || !array_key_exists($station['id'], $grants)) {
                continue;
            }
            $allowedChannels = $grants[$station['id']];
        }
        $channels = [];
        foreach (portal_station_channels_list($station['id']) as $ch) {
            if ($allowedChannels !== null && !in_array($ch['channel_base'], $allowedChannels, true)) {
                continue;
            }
            $channels[] = $ch;
        }
        if ($channels === []) {
            continue;
        }
        $out[] = [
            'id' => $station['id'],
            'name' => $station['name'],
            'status' => $station['status'],
            'health' => $station['health'] ?? 'unknown',
            'edge_base_url' => $station['edge_base_url'] ?? '',
            'channels' => $channels,
        ];
    }
    return $out;
}

/** True when $user (org-scoped) may view $channelBase on $stationId. */
function portal_user_allows_channel(array $user, string $stationId, string $channelBase): bool {
    $station = portal_station_find_by_id($stationId);
    if ($station === null || $station['org_id'] !== $user['org_id']) {
        return false;
    }
    if (in_array($user['role'], ['org_admin', 'org_operator'], true)) {
        return true;
    }
    $grants = portal_user_channel_grants_map($user);
    if (!array_key_exists($stationId, $grants)) {
        return false;
    }
    $allowed = $grants[$stationId];
    return $allowed === null || in_array($channelBase, $allowed, true);
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

function portal_bypass_enabled(): bool {
    return getenv('NEXVUE_PORTAL_AUTH_BYPASS') === '1';
}

function portal_session_start(): void {
    if (session_status() === PHP_SESSION_ACTIVE) {
        return;
    }
    $secure = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
        || (isset($_SERVER['SERVER_PORT']) && (int)$_SERVER['SERVER_PORT'] === 443);
    session_name('nexvue_portal_session');
    session_set_cookie_params([
        'lifetime' => 0, 'path' => portal_base_path() . '/', 'secure' => $secure, 'httponly' => true, 'samesite' => 'Lax',
    ]);
    session_start();
}

function portal_session_release(): void {
    if (session_status() === PHP_SESSION_ACTIVE) {
        session_write_close();
    }
}

function portal_session_clear(): void {
    portal_session_start();
    $_SESSION = [];
    if (ini_get('session.use_cookies')) {
        $p = session_get_cookie_params();
        setcookie(session_name(), '', [
            'expires' => time() - 42000, 'path' => $p['path'], 'secure' => $p['secure'],
            'httponly' => $p['httponly'], 'samesite' => $p['samesite'] ?? 'Lax',
        ]);
    }
    session_destroy();
}

function portal_login_user(array $row): void {
    portal_session_start();
    session_regenerate_id(true);
    $_SESSION['user_id'] = $row['id'];
}

function portal_current_user(bool $forceDb = false): ?array {
    static $memo = null;
    static $memoSet = false;
    if ($forceDb) {
        $memo = null;
        $memoSet = false;
    }
    if ($memoSet) {
        return $memo;
    }
    portal_session_start();
    $uid = $_SESSION['user_id'] ?? null;
    if (!is_string($uid) || $uid === '') {
        $memoSet = true;
        $memo = null;
        return null;
    }
    $row = portal_user_find_by_id($uid);
    if ($row === null || !empty($row['disabled_at'])) {
        unset($_SESSION['user_id']);
        $memoSet = true;
        $memo = null;
        return null;
    }
    $memo = $row;
    $memoSet = true;
    return $row;
}

function portal_me_payload(): ?array {
    $user = null;
    try {
        $user = portal_try_nexapp_user();
    } catch (RuntimeException $e) {
        if ($e->getMessage() === 'forbidden') {
            throw $e;
        }
        $user = null;
    }
    $user = $user ?? portal_current_user();
    if ($user === null) {
        return null;
    }
    $pub = portal_user_row_public($user);
    $org = portal_org_find_by_id($user['org_id']);
    $pub['org_name'] = $org['name'] ?? null;
    return $pub;
}

/**
 * @param list<string> $roles
 * @return array current user row
 * @throws RuntimeException 'unauthorized' | 'forbidden'
 */
function portal_require_roles(array $roles): array {
    if (portal_bypass_enabled()) {
        $orgId = (string)(getenv('NEXVUE_PORTAL_BYPASS_ORG_ID') ?: '');
        return [
            'id' => 'bypass', 'org_id' => $orgId, 'username' => 'bypass',
            'role' => $roles[0] ?? 'org_admin', 'password_hash' => '', 'email' => null,
            'must_change_password' => 0, 'disabled_at' => null, 'created_at' => '', 'updated_at' => '',
        ];
    }
    $nexapp = portal_try_nexapp_user();
    $user = $nexapp ?? portal_current_user();
    if ($user === null) {
        throw new RuntimeException('unauthorized');
    }
    if (!in_array($user['role'], $roles, true)) {
        throw new RuntimeException('forbidden');
    }
    return $user;
}

function portal_require_any(): array {
    if (portal_bypass_enabled()) {
        return portal_require_roles(['org_viewer']);
    }
    $nexapp = portal_try_nexapp_user();
    if ($nexapp !== null) {
        return $nexapp;
    }
    $user = portal_current_user();
    if ($user === null) {
        throw new RuntimeException('unauthorized');
    }
    return $user;
}

function portal_test_auth_enabled(): bool {
    return getenv('NEXVUE_PORTAL_TEST_AUTH') === '1';
}

function portal_base_path(): string {
    $o = getenv('NEXVUE_PORTAL_BASE');
    if (is_string($o) && $o !== '') {
        $o = rtrim($o, '/');
        return $o === '' ? '' : $o;
    }
    return '/nexvue';
}

function portal_nexapp_lib_load(): void {
    static $done = false;
    if ($done) {
        return;
    }
    $done = true;
    $here = __DIR__ . '/nexvue-portal-nexapp.php';
    if (is_file($here)) {
        require_once $here;
    }
}

/**
 * @return array<string, mixed>|null
 */
function portal_try_nexapp_user(): ?array {
    portal_nexapp_lib_load();
    if (!function_exists('portal_nexapp_check_access')) {
        return null;
    }
    $access = portal_nexapp_check_access();
    if (empty($access['ok'])) {
        if ((int)($access['status'] ?? 0) === 403) {
            throw new RuntimeException('forbidden');
        }
        if ((int)($access['status'] ?? 0) === 503) {
            throw new RuntimeException('unauthorized');
        }
        return null;
    }
    return portal_nexapp_upsert_user($access);
}

/**
 * @param array{sub?:string,email?:string,name?:string,role?:string} $access
 */
function portal_nexapp_upsert_user(array $access): array {
    $sub = trim((string)($access['sub'] ?? ''));
    if ($sub === '') {
        throw new RuntimeException('unauthorized');
    }
    $email = trim((string)($access['email'] ?? ''));
    $catalog = portal_nexapp_normalize_catalog_role($access['role'] ?? 'user');
    $role = $catalog === 'admin' ? 'org_admin' : 'org_viewer';
    $identity = 'nexapp:' . $sub;
    $org = portal_default_org();
    $row = portal_user_find_by_identity_key($identity);
    if ($row === null && $email !== '') {
        $row = portal_user_find_by_email($email);
    }
    if ($row === null) {
        $username = portal_nexapp_username($email, $sub);
        $row = portal_user_create([
            'org_id' => $org['id'],
            'username' => $username,
            'password' => bin2hex(random_bytes(16)),
            'role' => $role,
            'email' => $email !== '' ? $email : null,
            'must_change_password' => false,
        ]);
    }
    $now = portal_now_iso();
    $db = portal_db();
    $st = $db->prepare(
        'UPDATE portal_users SET identity_key=:k, catalog_role=:cr, role=:r, email=:e, disabled_at=NULL, updated_at=:up WHERE id=:id'
    );
    $st->bindValue(':k', $identity, SQLITE3_TEXT);
    $st->bindValue(':cr', $catalog, SQLITE3_TEXT);
    $st->bindValue(':r', $role, SQLITE3_TEXT);
    $st->bindValue(':e', $email !== '' ? $email : null, $email === '' ? SQLITE3_NULL : SQLITE3_TEXT);
    $st->bindValue(':up', $now, SQLITE3_TEXT);
    $st->bindValue(':id', $row['id'], SQLITE3_TEXT);
    $st->execute();
    $out = portal_user_find_by_id($row['id']);
    if ($out === null) {
        throw new RuntimeException('nexapp upsert failed');
    }
    portal_login_user($out);
    return $out;
}

function portal_nexapp_username(string $email, string $sub): string {
    $base = $email !== '' ? $email : ('nexapp-' . substr(preg_replace('/[^A-Za-z0-9]/', '', $sub) ?: hash('sha256', $sub), 0, 24));
    try {
        return portal_normalize_username($base);
    } catch (InvalidArgumentException $e) {
        return portal_normalize_username('nexapp-' . substr(hash('sha256', $sub), 0, 16));
    }
}

function portal_default_org(): array {
    $db = portal_db();
    $r = $db->query('SELECT * FROM orgs ORDER BY created_at ASC LIMIT 1');
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    if ($row) {
        return $row;
    }
    $name = getenv('NEXVUE_PORTAL_SEED_ORG_NAME') ?: 'Default Org';
    return portal_org_create((string)$name);
}

function portal_user_find_by_identity_key(string $key): ?array {
    if ($key === '') {
        return null;
    }
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM portal_users WHERE identity_key = :k LIMIT 1');
    $st->bindValue(':k', $key, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function portal_user_find_by_email(string $email): ?array {
    $email = trim($email);
    if ($email === '') {
        return null;
    }
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM portal_users WHERE email = :e COLLATE NOCASE LIMIT 1');
    $st->bindValue(':e', $email, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_ASSOC) : false;
    return $row ?: null;
}

function portal_normalize_edge_sync_role(string $role): string {
    $role = strtolower(trim($role));
    if (!in_array($role, NEXVUE_PORTAL_EDGE_SYNC_ROLES, true)) {
        return 'viewer';
    }
    return $role;
}

/**
 * @param list<string>|null $channels null = all channels on the station
 */
function portal_group_acl_put(string $orgId, string $groupId, string $groupName, string $stationId, ?array $channels, string $edgeRole): void {
    $groupId = trim($groupId);
    if ($groupId === '') {
        throw new InvalidArgumentException('nexapp_group_id required');
    }
    $station = portal_station_find_by_id($stationId);
    if ($station === null || $station['org_id'] !== $orgId) {
        throw new InvalidArgumentException('station not found in org');
    }
    $edgeRole = portal_normalize_edge_sync_role($edgeRole);
    $db = portal_db();
    $del = $db->prepare('DELETE FROM group_station_acl WHERE nexapp_group_id = :g AND station_id = :s');
    $del->bindValue(':g', $groupId, SQLITE3_TEXT);
    $del->bindValue(':s', $stationId, SQLITE3_TEXT);
    $del->execute();
    $rows = $channels === null ? [null] : array_values(array_unique($channels));
    foreach ($rows as $base) {
        if ($base !== null && !preg_match('/^ch[0-7]$/', (string)$base)) {
            throw new InvalidArgumentException('invalid channel: ' . $base);
        }
        $st = $db->prepare(
            'INSERT INTO group_station_acl (id, org_id, nexapp_group_id, nexapp_group_name, station_id, channel_base, edge_role, created_at)
             VALUES (:id, :org, :g, :n, :s, :c, :r, :cr)'
        );
        $st->bindValue(':id', portal_uuid(), SQLITE3_TEXT);
        $st->bindValue(':org', $orgId, SQLITE3_TEXT);
        $st->bindValue(':g', $groupId, SQLITE3_TEXT);
        $st->bindValue(':n', $groupName, SQLITE3_TEXT);
        $st->bindValue(':s', $stationId, SQLITE3_TEXT);
        $st->bindValue(':c', $base, $base === null ? SQLITE3_NULL : SQLITE3_TEXT);
        $st->bindValue(':r', $edgeRole, SQLITE3_TEXT);
        $st->bindValue(':cr', portal_now_iso(), SQLITE3_TEXT);
        $st->execute();
    }
}

/** @return list<array<string,mixed>> */
function portal_group_acl_list(string $orgId): array {
    $db = portal_db();
    $st = $db->prepare('SELECT * FROM group_station_acl WHERE org_id = :o ORDER BY nexapp_group_name, station_id');
    $st->bindValue(':o', $orgId, SQLITE3_TEXT);
    $r = $st->execute();
    $out = [];
    if ($r) {
        while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
            $out[] = [
                'id' => $row['id'],
                'nexapp_group_id' => $row['nexapp_group_id'],
                'nexapp_group_name' => $row['nexapp_group_name'],
                'station_id' => $row['station_id'],
                'channel_base' => $row['channel_base'],
                'edge_role' => $row['edge_role'],
            ];
        }
    }
    return $out;
}

/**
 * station_id => null (all channels) | list of channel bases
 *
 * @return array<string, list<string>|null>
 */
function portal_user_channel_grants_map(array $user): array {
    $grants = [];
    $db = portal_db();
    $st = $db->prepare('SELECT station_id, channel_base FROM catalog_acl WHERE portal_user_id = :u');
    $st->bindValue(':u', $user['id'], SQLITE3_TEXT);
    $r = $st->execute();
    if ($r) {
        while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
            portal_grants_map_add($grants, (string)$row['station_id'], $row['channel_base']);
        }
    }
    $identity = (string)($user['identity_key'] ?? '');
    $sub = str_starts_with($identity, 'nexapp:') ? substr($identity, 7) : '';
    if ($sub !== '') {
        portal_nexapp_lib_load();
        $gids = function_exists('portal_nexapp_group_ids_for_sub') ? portal_nexapp_group_ids_for_sub($sub) : [];
        foreach ($gids as $gid) {
            $gst = $db->prepare('SELECT station_id, channel_base FROM group_station_acl WHERE nexapp_group_id = :g');
            $gst->bindValue(':g', $gid, SQLITE3_TEXT);
            $gr = $gst->execute();
            if ($gr) {
                while ($row = $gr->fetchArray(SQLITE3_ASSOC)) {
                    portal_grants_map_add($grants, (string)$row['station_id'], $row['channel_base']);
                }
            }
        }
    }
    return $grants;
}

/**
 * @param array<string, list<string>|null> $grants
 */
function portal_grants_map_add(array &$grants, string $stationId, mixed $channelBase): void {
    if ($channelBase === null || $channelBase === '') {
        $grants[$stationId] = null;
        return;
    }
    if (array_key_exists($stationId, $grants) && $grants[$stationId] === null) {
        return;
    }
    $grants[$stationId][] = (string)$channelBase;
}

function portal_edge_role_rank(string $role): int {
    return match (portal_normalize_edge_sync_role($role)) {
        'operator' => 3,
        'sharer' => 2,
        default => 1,
    };
}

/**
 * Users the edge should materialize for this station. Null directory →
 * omit sync (caller must not send users_sync).
 *
 * @return null|list<array<string,mixed>>
 */
function portal_heartbeat_users_for_station(string $stationId): ?array {
    portal_nexapp_lib_load();
    if (!function_exists('portal_nexapp_directory')) {
        return null;
    }
    $directory = portal_nexapp_directory();
    if ($directory === null) {
        return null;
    }
    $db = portal_db();
    /** @var array<string, array{sub:string,email:string,name:string,role:string,channels:?list<string>}> $bySub */
    $bySub = [];
    foreach ($directory as $g) {
        $gst = $db->prepare('SELECT channel_base, edge_role FROM group_station_acl WHERE nexapp_group_id = :g AND station_id = :s');
        $gst->bindValue(':g', $g['id'], SQLITE3_TEXT);
        $gst->bindValue(':s', $stationId, SQLITE3_TEXT);
        $gr = $gst->execute();
        $groupChannels = [];
        $groupAll = false;
        $groupRole = 'viewer';
        $any = false;
        if ($gr) {
            while ($row = $gr->fetchArray(SQLITE3_ASSOC)) {
                $any = true;
                $groupRole = portal_edge_role_rank($row['edge_role']) > portal_edge_role_rank($groupRole)
                    ? portal_normalize_edge_sync_role((string)$row['edge_role'])
                    : $groupRole;
                if ($row['channel_base'] === null || $row['channel_base'] === '') {
                    $groupAll = true;
                } else {
                    $groupChannels[] = (string)$row['channel_base'];
                }
            }
        }
        if (!$any) {
            continue;
        }
        $ch = $groupAll ? null : array_values(array_unique($groupChannels));
        foreach ($g['members'] as $m) {
            $sub = $m['sub'];
            if ($sub === '') {
                continue;
            }
            if (!isset($bySub[$sub])) {
                $bySub[$sub] = [
                    'sub' => $sub,
                    'email' => $m['email'],
                    'name' => $m['name'],
                    'role' => $groupRole,
                    'channels' => $ch,
                ];
                continue;
            }
            if (portal_edge_role_rank($groupRole) > portal_edge_role_rank($bySub[$sub]['role'])) {
                $bySub[$sub]['role'] = $groupRole;
            }
            if ($bySub[$sub]['channels'] === null || $ch === null) {
                $bySub[$sub]['channels'] = null;
            } else {
                $bySub[$sub]['channels'] = array_values(array_unique(array_merge($bySub[$sub]['channels'], $ch)));
            }
        }
    }
    $out = [];
    foreach ($bySub as $u) {
        $id = $u['sub'];
        $email = $u['email'];
        $username = $email !== '' ? $email : ('nexapp-' . substr(hash('sha256', $id), 0, 12));
        $out[] = [
            'id' => $id,
            'username' => $username,
            'email' => $email !== '' ? $email : null,
            'identity_key' => 'nexapp:' . $id,
            'role' => portal_normalize_edge_sync_role($u['role']),
            'channels' => $u['channels'],
            'disabled_at' => null,
            'must_change_password' => false,
        ];
    }
    return $out;
}

function portal_mint_sso_jwt(array $user, string $stationId): string {
    $station = portal_station_find_by_id($stationId);
    if ($station === null || $station['org_id'] !== $user['org_id']) {
        throw new InvalidArgumentException('station not found');
    }
    $role = 'viewer';
    $channels = null;
    if (in_array($user['role'], ['org_admin', 'org_operator'], true)) {
        $role = 'operator';
        $channels = null;
    } else {
        $grants = portal_user_channel_grants_map($user);
        if (!array_key_exists($stationId, $grants)) {
            throw new RuntimeException('forbidden');
        }
        $channels = $grants[$stationId];
        $identity = (string)($user['identity_key'] ?? '');
        $sub = str_starts_with($identity, 'nexapp:') ? substr($identity, 7) : '';
        if ($sub !== '') {
            $db = portal_db();
            $gids = portal_nexapp_group_ids_for_sub($sub);
            foreach ($gids as $gid) {
                $st = $db->prepare('SELECT edge_role FROM group_station_acl WHERE nexapp_group_id = :g AND station_id = :s');
                $st->bindValue(':g', $gid, SQLITE3_TEXT);
                $st->bindValue(':s', $stationId, SQLITE3_TEXT);
                $r = $st->execute();
                if ($r) {
                    while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
                        if (portal_edge_role_rank((string)$row['edge_role']) > portal_edge_role_rank($role)) {
                            $role = portal_normalize_edge_sync_role((string)$row['edge_role']);
                        }
                    }
                }
            }
        }
    }
    $identity = (string)($user['identity_key'] ?? '');
    $nexappSub = str_starts_with($identity, 'nexapp:') ? substr($identity, 7) : '';
    return portal_jwt_encode([
        'sub' => 'portal:' . $user['id'],
        'typ' => 'nexvue-portal-sso',
        'email' => $user['email'] ?? '',
        'name' => $user['username'] ?? '',
        'nexapp_sub' => $nexappSub,
        'station_id' => $stationId,
        'role' => $role,
        'channels' => $channels,
    ], NEXVUE_PORTAL_SSO_JWT_TTL_S);
}

function portal_station_login_url(array $station, string $jwt): string {
    $base = rtrim((string)$station['edge_base_url'], '/');
    return $base . '/login#portal_sso=' . rawurlencode($jwt);
}

function portal_health_summary(string $orgId): array {
    $stations = portal_stations_list_for_org($orgId);
    $ok = 0;
    $down = 0;
    $unknown = 0;
    foreach ($stations as $s) {
        $h = $s['health'] ?? 'unknown';
        if ($h === 'ok') {
            $ok++;
        } elseif ($h === 'down') {
            $down++;
        } else {
            $unknown++;
        }
    }
    return [
        'stations' => count($stations),
        'ok' => $ok,
        'down' => $down,
        'unknown' => $unknown,
    ];
}

