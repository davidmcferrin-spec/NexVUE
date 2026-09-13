<?php
/**
 * NexAPP portal widget — station health counts from last heartbeat.
 * Same-host JSON endpoint declared in nexapp-manifest.json.
 */
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

require dirname(__DIR__) . '/nexvue-portal-auth-lib.php';

try {
    portal_migrate();
    $user = portal_require_any();
    $sum = portal_health_summary($user['org_id']);
    $label = $sum['ok'] . ' up';
    if ($sum['down'] > 0) {
        $label .= ' · ' . $sum['down'] . ' stale';
    }
    echo json_encode([
        'ok' => true,
        'value' => (string) $sum['stations'],
        'label' => $label,
        'stations' => $sum['stations'],
        'ok_count' => $sum['ok'],
        'down' => $sum['down'],
        'unknown' => $sum['unknown'],
    ], JSON_UNESCAPED_SLASHES);
} catch (RuntimeException $e) {
    $code = $e->getMessage() === 'forbidden' ? 403 : 401;
    http_response_code($code);
    echo json_encode(['ok' => false, 'error' => $e->getMessage()]);
} catch (Throwable $e) {
    http_response_code(500);
    echo json_encode(['ok' => false, 'error' => 'unavailable']);
}
