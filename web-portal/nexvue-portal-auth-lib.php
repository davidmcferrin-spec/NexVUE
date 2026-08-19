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
 * web-node/. A portal deployment is a separate box from an edge node (see
 * setup.sh --portal); this file has its own SQLite store, its own RSA
 * signing keypair, and its own bcrypt session model, styled identically to
 * web-node/nexvue-auth-lib.php but never sharing state with it. The one
 * hard wire contract shared between the two: portal_mint_viewer_jwt()'s
 * claim shape (mediamtx_permissions: [{action, path}]) must exactly match
 * what MediaMTX validates on the target edge — that's enforced by
 * convention, not by code sharing.
 *
 * SQLite: /var/lib/nexvue-portal/portal.db (override NEXVUE_PORTAL_DB)
 * Keys:   /var/lib/nexvue-portal/{private.pem,public.pem,jwks.json,kid}
 *         (override NEXVUE_PORTAL_DIR)
 *
 * Roles (org-scoped): org_admin | org_operator | org_viewer. org_admin and
 * org_operator implicitly see every station/channel in their org (no ACL
 * rows needed); org_viewer sees only what catalog_acl grants.
 */

declare(strict_types=1);

const NEXVUE_PORTAL_ROLES = ['org_admin', 'org_operator', 'org_viewer'];
const NEXVUE_PORTAL_VIEWER_JWT_TTL_S = 90;
const NEXVUE_PORTAL_SCHEMA_VERSION = 1;
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
            portal_user_create([
                'org_id' => $org['id'],
                'username' => 'admin',
                'password' => 'password',
                'role' => 'org_admin',
                'must_change_password' => true,
            ]);
        }
        $ver = 1;
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
    return [
        'id' => $row['id'],
        'org_id' => $row['org_id'],
        'name' => $row['name'],
        'edge_base_url' => $row['edge_base_url'],
        'edge_whep_port' => (int)$row['edge_whep_port'],
        'status' => $row['status'],
        'edge_version' => $row['edge_version'] ?: null,
        'last_heartbeat_at' => $row['last_heartbeat_at'] ?: null,
        'created_at' => $row['created_at'],
        'updated_at' => $row['updated_at'],
    ];
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
    $grants = null; // station_id => null(all) | list<channel_base>
    if (!$seeAll) {
        $grants = [];
        $db = portal_db();
        $st = $db->prepare('SELECT station_id, channel_base FROM catalog_acl WHERE portal_user_id = :u');
        $st->bindValue(':u', $user['id'], SQLITE3_TEXT);
        $r = $st->execute();
        if ($r) {
            while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
                $sid = (string)$row['station_id'];
                if ($row['channel_base'] === null) {
                    $grants[$sid] = null; // all channels
                } elseif (!array_key_exists($sid, $grants) || $grants[$sid] !== null) {
                    $grants[$sid][] = (string)$row['channel_base'];
                }
            }
        }
    }
    $out = [];
    foreach ($stations as $station) {
        if ($station['status'] !== 'active') {
            continue;
        }
        $allowedChannels = null;
        if (!$seeAll) {
            if (!array_key_exists($station['id'], $grants)) {
                continue; // no grant at all on this station
            }
            $allowedChannels = $grants[$station['id']]; // null = all
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
    $db = portal_db();
    $st = $db->prepare(
        'SELECT COUNT(*) FROM catalog_acl WHERE portal_user_id = :u AND station_id = :s AND (channel_base IS NULL OR channel_base = :c)'
    );
    $st->bindValue(':u', $user['id'], SQLITE3_TEXT);
    $st->bindValue(':s', $stationId, SQLITE3_TEXT);
    $st->bindValue(':c', $channelBase, SQLITE3_TEXT);
    $r = $st->execute();
    $row = $r ? $r->fetchArray(SQLITE3_NUM) : false;
    return $row !== false && (int)$row[0] > 0;
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
        'lifetime' => 0, 'path' => '/', 'secure' => $secure, 'httponly' => true, 'samesite' => 'Lax',
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
    $user = portal_current_user();
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
    $user = portal_current_user();
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
    $user = portal_current_user();
    if ($user === null) {
        throw new RuntimeException('unauthorized');
    }
    return $user;
}
