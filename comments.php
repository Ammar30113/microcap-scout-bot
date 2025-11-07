<?php
declare(strict_types=1);

require_once __DIR__ . '/lib/csrf.php';
require_once __DIR__ . '/lib/db.php';

ensure_session_started();
header('Content-Type: application/json');

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'Method Not Allowed']);
    exit;
}

$payload = json_decode(file_get_contents('php://input'), true);
if (!is_array($payload)) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid JSON payload.']);
    exit;
}

if (!verify_csrf_token($payload['csrf_token'] ?? null)) {
    http_response_code(422);
    echo json_encode([
        'error' => 'Invalid CSRF token.',
        'csrfToken' => rotate_csrf_token(),
    ]);
    exit;
}

$nickname = isset($payload['nickname']) ? (string) $payload['nickname'] : '';
$body = isset($payload['body']) ? (string) $payload['body'] : '';
$messageId = isset($payload['message_id']) ? (int) $payload['message_id'] : 0;

try {
    $pdo = db();
    $comment = insert_comment($pdo, $messageId, $nickname, $body);
} catch (\InvalidArgumentException $exception) {
    http_response_code(422);
    echo json_encode([
        'error' => $exception->getMessage(),
        'csrfToken' => rotate_csrf_token(),
    ]);
    exit;
} catch (\RuntimeException $exception) {
    http_response_code(500);
    echo json_encode([
        'error' => 'Failed to save comment.',
        'csrfToken' => rotate_csrf_token(),
    ]);
    exit;
}

http_response_code(201);
echo json_encode([
    'comment' => $comment,
    'csrfToken' => rotate_csrf_token(),
]);
