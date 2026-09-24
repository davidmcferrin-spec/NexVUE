<?php
/**
 * NexAPP identity helper for the NexVUE portal Alias at /nexvue.
 *
 * Verify RS256 (PEM on this VM), then live grants via AccessService or
 * GET /api/access.php. Catalog role is user|admin ceiling only.
 *
 * Tests may stub access / directory with env file paths — never set those
 * on a production box.
 */

declare(strict_types=1);

const NEXVUE_NEXAPP_SERVICE_ID = 'nexvue';
/** Hub AuthCookie::read order: configured name, then legacy. */
const NEXVUE_NEXAPP_COOKIE = '__Host-NexAPP_AUTH';
const NEXVUE_NEXAPP_COOKIE_LEGACY = 'NexAPP_AUTH';
/** Hub bootstrap never calls session_name(); php.ini default is PHPSESSID. */
const NEXVUE_NEXAPP_HUB_SESSION = 'PHPSESSID';
const NEXVUE_NEXAPP_ISSUER_DEFAULT = 'https://nexapp.nexstar.tv';
const NEXVUE_NEXAPP_PUBLIC_KEY_DEFAULT = '/etc/nexvue-portal/jwt_public.pem';
const NEXVUE_NEXAPP_ROOT_DEFAULT = '/var/www/nexapp';
const NEXVUE_NEXAPP_ACCESS_URL_DEFAULT = 'https://nexapp.nexstar.tv/api/access.php';
const NEXVUE_NEXAPP_LOGIN_DEFAULT = 'https://nexapp.nexstar.tv/login.php';
const NEXVUE_NEXAPP_LOGOUT_DEFAULT = 'https://nexapp.nexstar.tv/logout.php';

function portal_nexapp_issuer(): string {
    $o = getenv('NEXAPP_ISSUER');
    return (is_string($o) && $o !== '') ? $o : NEXVUE_NEXAPP_ISSUER_DEFAULT;
}

function portal_nexapp_public_key_path(): string {
    $o = getenv('NEXAPP_PUBLIC_KEY_PATH');
    if (is_string($o) && $o !== '') {
        return $o;
    }
    $copied = NEXVUE_NEXAPP_PUBLIC_KEY_DEFAULT;
    if (is_readable($copied)) {
        return $copied;
    }
    return '/var/www/nexapp/keys/jwt_public.pem';
}

function portal_nexapp_root(): string {
    $o = getenv('NEXAPP_ROOT');
    return (is_string($o) && $o !== '') ? rtrim($o, '/\\') : NEXVUE_NEXAPP_ROOT_DEFAULT;
}

function portal_nexapp_login_url(?string $next = null): string {
    $o = getenv('NEXAPP_LOGIN_URL');
    $base = (is_string($o) && $o !== '') ? $o : NEXVUE_NEXAPP_LOGIN_DEFAULT;
    $return = '/nexvue/';
    if (is_string($next) && str_starts_with($next, '/') && !str_starts_with($next, '//')) {
        $return = $next;
    }
    $join = str_contains($base, '?') ? '&' : '?';
    return $base . $join . 'return=' . rawurlencode($return);
}

function portal_nexapp_logout_url(): string {
    $o = getenv('NEXAPP_LOGOUT_URL');
    return (is_string($o) && $o !== '') ? $o : NEXVUE_NEXAPP_LOGOUT_DEFAULT;
}

/**
 * Browser sign-out fields. Hub GET /logout.php is 405; the client must POST
 * `csrf` + `return`. csrf is null when the hub PHP session cookie is absent.
 *
 * @return array{logout_url: string, csrf: ?string, return_to: string}
 */
function portal_nexapp_logout_client(): array {
    return [
        'logout_url' => portal_nexapp_logout_url(),
        'csrf' => portal_nexapp_hub_csrf_token(),
        'return_to' => '/login.php',
    ];
}

/**
 * CSRF lives in the hub PHP session ($_SESSION['csrf']), not the portal session.
 * Read that session in place. Do not mint a new PHPSESSID (a Set-Cookie from
 * this Alias would shadow the hub cookie on /nexvue/).
 */
function portal_nexapp_hub_csrf_token(): ?string {
    $cookie = $_COOKIE[NEXVUE_NEXAPP_HUB_SESSION] ?? null;
    if (!is_string($cookie) || preg_match('/^[A-Za-z0-9,-]{1,128}$/', $cookie) !== 1) {
        return null;
    }

    $priorActive = session_status() === PHP_SESSION_ACTIVE;
    $priorName = session_name();
    $priorId = $priorActive ? session_id() : '';
    $priorData = $priorActive ? $_SESSION : null;
    $priorParams = session_get_cookie_params();
    $priorUseCookies = (string) ini_get('session.use_cookies');
    if ($priorActive) {
        session_write_close();
    }

    $token = null;
    try {
        ini_set('session.use_cookies', '0');
        session_name(NEXVUE_NEXAPP_HUB_SESSION);
        session_id($cookie);
        $started = session_start();
        if ($started === true && session_id() === $cookie) {
            $minted = false;
            if (!isset($_SESSION['csrf']) || !is_string($_SESSION['csrf']) || $_SESSION['csrf'] === '') {
                $_SESSION['csrf'] = bin2hex(random_bytes(16));
                $minted = true;
            }
            $token = $_SESSION['csrf'];
            if ($minted) {
                session_write_close();
            } else {
                session_abort();
            }
        } elseif (session_status() === PHP_SESSION_ACTIVE) {
            $_SESSION = [];
            session_destroy();
        }
    } catch (Throwable $e) {
        $token = null;
    }
    if (session_status() === PHP_SESSION_ACTIVE) {
        session_write_close();
    }

    ini_set('session.use_cookies', $priorUseCookies);
    session_name($priorName !== '' ? $priorName : 'nexvue_portal_session');
    session_set_cookie_params([
        'lifetime' => (int) ($priorParams['lifetime'] ?? 0),
        'path' => (string) ($priorParams['path'] ?? '/'),
        'domain' => (string) ($priorParams['domain'] ?? ''),
        'secure' => (bool) ($priorParams['secure'] ?? false),
        'httponly' => (bool) ($priorParams['httponly'] ?? true),
        'samesite' => (string) ($priorParams['samesite'] ?? 'Lax'),
    ]);
    if ($priorActive && $priorId !== '') {
        session_id($priorId);
        if (session_start() === true && is_array($priorData)) {
            $_SESSION = $priorData;
        }
    }
    return is_string($token) && $token !== '' ? $token : null;
}

function portal_nexapp_token_from_request(): ?string {
    $header = $_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '';
    if (is_string($header) && preg_match('/^Bearer\s+(\S+)/i', $header, $m) === 1) {
        return $m[1];
    }
    foreach ([NEXVUE_NEXAPP_COOKIE, NEXVUE_NEXAPP_COOKIE_LEGACY] as $name) {
        $cookie = $_COOKIE[$name] ?? null;
        if (is_string($cookie) && $cookie !== '') {
            return $cookie;
        }
    }
    return null;
}

function portal_nexapp_b64url_decode(string $data): string {
    $remainder = strlen($data) % 4;
    if ($remainder) {
        $data .= str_repeat('=', 4 - $remainder);
    }
    $out = base64_decode(strtr($data, '-_', '+/'), true);
    if ($out === false) {
        throw new RuntimeException('bad b64');
    }
    return $out;
}

/** RS256 + exp + iss. Throws on failure. Never accept a cookie because the PEM is missing. */
function portal_nexapp_verify_jwt(string $jwt): object {
    $pemPath = portal_nexapp_public_key_path();
    if (!is_readable($pemPath)) {
        throw new RuntimeException('JWT public key unreadable');
    }
    $parts = explode('.', $jwt);
    if (count($parts) !== 3) {
        throw new RuntimeException('Malformed JWT');
    }
    [$h64, $p64, $s64] = $parts;
    $header = json_decode(portal_nexapp_b64url_decode($h64), false);
    $payload = json_decode(portal_nexapp_b64url_decode($p64), false);
    if (!$header || !$payload || ($header->alg ?? '') !== 'RS256') {
        throw new RuntimeException('Bad JWT header/payload');
    }
    $pem = (string) file_get_contents($pemPath);
    $ok = openssl_verify("$h64.$p64", portal_nexapp_b64url_decode($s64), $pem, OPENSSL_ALGO_SHA256);
    if ($ok !== 1) {
        throw new RuntimeException('Invalid signature');
    }
    if (!isset($payload->exp) || time() >= (int) $payload->exp) {
        throw new RuntimeException('Expired');
    }
    $issuer = portal_nexapp_issuer();
    if ($issuer !== '' && (string) ($payload->iss ?? '') !== $issuer) {
        throw new RuntimeException('Bad issuer');
    }
    return $payload;
}

function portal_nexapp_normalize_catalog_role(mixed $role): string {
    return $role === 'admin' ? 'admin' : 'user';
}

/**
 * @return array{ok: bool, status: int, error?: string, sub?: string, email?: string, name?: string, role?: string, source?: string}
 */
function portal_nexapp_check_access(?string $token = null): array {
    $stub = getenv('NEXVUE_PORTAL_NEXAPP_ACCESS_STUB');
    if (is_string($stub) && $stub !== '' && is_readable($stub)) {
        $raw = json_decode((string) file_get_contents($stub), true);
        if (is_array($raw)) {
            $raw['ok'] = !empty($raw['ok']);
            $raw['status'] = (int) ($raw['status'] ?? ($raw['ok'] ? 200 : 401));
            if (!empty($raw['role'])) {
                $raw['role'] = portal_nexapp_normalize_catalog_role($raw['role']);
            }
            return $raw;
        }
    }

    $token = $token ?? portal_nexapp_token_from_request();
    if ($token === null || $token === '') {
        return ['ok' => false, 'status' => 401, 'error' => 'unauthorized'];
    }

    try {
        portal_nexapp_verify_jwt($token);
    } catch (Throwable $e) {
        return ['ok' => false, 'status' => 401, 'error' => 'unauthorized'];
    }

    $hubRoot = portal_nexapp_root();
    $bootstrap = $hubRoot . '/src/bootstrap.php';
    if (is_readable($bootstrap)) {
        require_once $bootstrap;
        $result = (new \NexApp\Auth\AccessService())->check($token, NEXVUE_NEXAPP_SERVICE_ID);
        unset($result['user']);
        if (!empty($result['role'])) {
            $result['role'] = portal_nexapp_normalize_catalog_role($result['role']);
        }
        return $result;
    }

    $url = getenv('NEXAPP_ACCESS_URL');
    $endpointBase = (is_string($url) && $url !== '') ? $url : NEXVUE_NEXAPP_ACCESS_URL_DEFAULT;
    $endpoint = $endpointBase . (str_contains($endpointBase, '?') ? '&' : '?')
        . 'service_id=' . rawurlencode(NEXVUE_NEXAPP_SERVICE_ID);
    $ctx = stream_context_create([
        'http' => [
            'method' => 'GET',
            'header' => "Authorization: Bearer {$token}\r\nAccept: application/json\r\n",
            'timeout' => 3,
            'ignore_errors' => true,
        ],
        'ssl' => [
            'verify_peer' => true,
            'verify_peer_name' => true,
        ],
    ]);
    $body = @file_get_contents($endpoint, false, $ctx);
    $status = 0;
    if (isset($http_response_header[0]) && preg_match('/\s(\d{3})\b/', $http_response_header[0], $m) === 1) {
        $status = (int) $m[1];
    }
    if ($body === false || $status === 0) {
        return ['ok' => false, 'status' => 503, 'error' => 'introspect_failed'];
    }
    $data = json_decode($body, true);
    if (!is_array($data)) {
        return ['ok' => false, 'status' => 503, 'error' => 'introspect_failed'];
    }
    $data['status'] = $status;
    $data['ok'] = ($status === 200 && !empty($data['ok']));
    if (!empty($data['role'])) {
        $data['role'] = portal_nexapp_normalize_catalog_role($data['role']);
    }
    unset($data['user']);
    return $data;
}

/**
 * Groups granted to NexVUE plus members. Same-VM hub bootstrap, or a test
 * JSON stub. Returns null when the directory is unavailable (heartbeat
 * must omit users_sync rather than wipe nodes).
 *
 * @return null|list<array{id:string,name:string,catalog_role:string,members:list<array{sub:string,email:string,name:string}>}>
 */
function portal_nexapp_directory(): ?array {
    $stub = getenv('NEXVUE_PORTAL_NEXAPP_DIRECTORY');
    if (is_string($stub) && $stub !== '' && is_readable($stub)) {
        $raw = json_decode((string) file_get_contents($stub), true);
        if (!is_array($raw)) {
            return [];
        }
        return portal_nexapp_directory_normalize($raw);
    }

    $hubRoot = portal_nexapp_root();
    $bootstrap = $hubRoot . '/src/bootstrap.php';
    if (!is_readable($bootstrap)) {
        return null;
    }
    require_once $bootstrap;
    try {
        $grants = (new \NexApp\Services\ServiceGrantRepository())->grantsForService(NEXVUE_NEXAPP_SERVICE_ID);
        $groups = new \NexApp\Users\GroupRepository();
        $out = [];
        foreach ($grants as $gid => $role) {
            $g = $groups->findById((string) $gid);
            if ($g === null) {
                continue;
            }
            $members = [];
            foreach ($groups->members((string) $gid) as $u) {
                if (empty($u['is_active'])) {
                    continue;
                }
                $members[] = [
                    'sub' => (string) ($u['id'] ?? ''),
                    'email' => (string) ($u['email'] ?? ''),
                    'name' => (string) ($u['display_name'] ?? ''),
                ];
            }
            $out[] = [
                'id' => (string) $g['id'],
                'name' => (string) ($g['name'] ?? ''),
                'catalog_role' => portal_nexapp_normalize_catalog_role($role),
                'members' => $members,
            ];
        }
        return $out;
    } catch (Throwable $e) {
        return null;
    }
}

/**
 * @param list<mixed> $raw
 * @return list<array{id:string,name:string,catalog_role:string,members:list<array{sub:string,email:string,name:string}>}>
 */
function portal_nexapp_directory_normalize(array $raw): array {
    $out = [];
    foreach ($raw as $g) {
        if (!is_array($g) || empty($g['id'])) {
            continue;
        }
        $members = [];
        $rawMembers = $g['members'] ?? [];
        if (is_array($rawMembers)) {
            foreach ($rawMembers as $m) {
                if (!is_array($m) || empty($m['sub'])) {
                    continue;
                }
                $members[] = [
                    'sub' => (string) $m['sub'],
                    'email' => (string) ($m['email'] ?? ''),
                    'name' => (string) ($m['name'] ?? ''),
                ];
            }
        }
        $out[] = [
            'id' => (string) $g['id'],
            'name' => (string) ($g['name'] ?? ''),
            'catalog_role' => portal_nexapp_normalize_catalog_role($g['catalog_role'] ?? 'user'),
            'members' => $members,
        ];
    }
    return $out;
}

/** @return list<string> */
function portal_nexapp_group_ids_for_sub(string $sub, ?array $directory = null): array {
    $directory = $directory ?? portal_nexapp_directory();
    if ($directory === null) {
        return [];
    }
    $ids = [];
    foreach ($directory as $g) {
        foreach ($g['members'] as $m) {
            if ($m['sub'] === $sub) {
                $ids[] = $g['id'];
                break;
            }
        }
    }
    return $ids;
}
