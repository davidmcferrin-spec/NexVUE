<?php
/**
 * nexvue-version.php — public version stamp for the top-nav badge.
 *
 * Reads VERSION from the docroot (installed by setup.sh) and optional
 * /var/lib/nexvue/version.json (written by nexvue-ops-update.sh / setup.sh).
 * No sudo. Cache-friendly short TTL via Cache-Control.
 */
declare(strict_types=1);

header('Content-Type: application/json');
header('Cache-Control: no-store');

function read_version_file(string $path): string {
    if (!is_file($path) || !is_readable($path)) {
        return '';
    }
    $raw = trim((string)file_get_contents($path));
    if ($raw === '' || !preg_match('/^\d+\.\d+\.\d+([.-][A-Za-z0-9.-]+)?$/', $raw)) {
        return '';
    }
    return $raw;
}

$version = read_version_file(__DIR__ . '/VERSION');
if ($version === '') {
    $version = read_version_file('/usr/local/share/nexvue/VERSION');
}
if ($version === '') {
    $version = '0.0.0';
}

$gitSha = '';
$gitBranch = '';
$repo = '';
$updatedAt = '';

$stamp = '/var/lib/nexvue/version.json';
if (is_file($stamp) && is_readable($stamp)) {
    $data = json_decode((string)file_get_contents($stamp), true);
    if (is_array($data)) {
        if ($version === '0.0.0' && !empty($data['version'])) {
            $v = trim((string)$data['version']);
            if (preg_match('/^\d+\.\d+\.\d+([.-][A-Za-z0-9.-]+)?$/', $v)) {
                $version = $v;
            }
        }
        $gitSha = isset($data['git_sha']) ? substr(preg_replace('/[^0-9a-fA-F]/', '', (string)$data['git_sha']) ?? '', 0, 16) : '';
        $gitBranch = isset($data['git_branch']) ? substr(preg_replace('/[^A-Za-z0-9._\/-]/', '', (string)$data['git_branch']) ?? '', 0, 64) : '';
        $repo = isset($data['repo']) ? substr((string)$data['repo'], 0, 256) : '';
        $updatedAt = isset($data['updated_at']) ? substr((string)$data['updated_at'], 0, 40) : '';
    }
}

echo json_encode([
    'ok' => true,
    'version' => $version,
    'git_sha' => $gitSha,
    'git_branch' => $gitBranch,
    'repo' => $repo,
    'updated_at' => $updatedAt,
], JSON_UNESCAPED_SLASHES);
