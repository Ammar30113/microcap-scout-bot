<?php
declare(strict_types=1);

/**
 * Return a singleton PDO connection configured from environment variables.
 */
function db(): \PDO
{
    static $pdo = null;

    if ($pdo instanceof \PDO) {
        return $pdo;
    }

    $dsn = getenv('DB_DSN');
    $user = getenv('DB_USER');
    $password = getenv('DB_PASSWORD');

    if ($dsn === false || $dsn === '') {
        throw new \RuntimeException('Database DSN is not configured (DB_DSN).');
    }

    try {
        $pdo = new \PDO(
            $dsn,
            $user === false ? null : $user,
            $password === false ? null : $password,
            [
                \PDO::ATTR_ERRMODE => \PDO::ERRMODE_EXCEPTION,
                \PDO::ATTR_DEFAULT_FETCH_MODE => \PDO::FETCH_ASSOC,
                \PDO::ATTR_EMULATE_PREPARES => false,
            ],
        );
    } catch (\PDOException $exception) {
        throw new \RuntimeException('Unable to connect to the database: ' . $exception->getMessage(), 0, $exception);
    }

    return $pdo;
}

/**
 * Load every comment associated with the supplied message identifier.
 */
function fetch_comments(\PDO $pdo, int $messageId): array
{
    $statement = $pdo->prepare(
        'SELECT id, message_id, nickname, body, created_at FROM comments WHERE message_id = :message_id ORDER BY created_at ASC, id ASC'
    );
    $statement->execute([':message_id' => $messageId]);

    return $statement->fetchAll();
}

/**
 * Attach comment collections to each message in the provided list.
 *
 * Each message is expected to expose an integer `id` key. The function returns a
 * new array that mirrors the original payload while enriching every message with
 * a `comments` key containing the ordered comment list.
 */
function hydrate_messages_with_comments(\PDO $pdo, array $messages): array
{
    if ($messages === []) {
        return [];
    }

    $messageIds = [];
    foreach ($messages as $message) {
        if (!isset($message['id'])) {
            continue;
        }
        $messageIds[] = (int) $message['id'];
    }

    $messageIds = array_values(array_unique(array_filter($messageIds)));
    if ($messageIds === []) {
        return array_map(
            static function (array $message): array {
                if (!array_key_exists('comments', $message)) {
                    $message['comments'] = [];
                }

                return $message;
            },
            $messages,
        );
    }

    $placeholders = implode(',', array_fill(0, count($messageIds), '?'));
    $statement = $pdo->prepare(
        sprintf(
            'SELECT id, message_id, nickname, body, created_at FROM comments WHERE message_id IN (%s) ORDER BY message_id ASC, created_at ASC, id ASC',
            $placeholders,
        ),
    );
    $statement->execute($messageIds);

    $commentsByMessage = [];
    while ($row = $statement->fetch()) {
        $messageKey = (int) $row['message_id'];
        $commentsByMessage[$messageKey][] = $row;
    }

    foreach ($messages as &$message) {
        $messageId = isset($message['id']) ? (int) $message['id'] : 0;
        $message['comments'] = $commentsByMessage[$messageId] ?? [];
    }
    unset($message);

    return $messages;
}

/**
 * Persist a freshly submitted comment and return its stored representation.
 */
function insert_comment(\PDO $pdo, int $messageId, string $nickname, string $body): array
{
    $nickname = trim($nickname);
    $body = trim($body);

    if ($messageId <= 0) {
        throw new \InvalidArgumentException('A valid message identifier is required.');
    }

    if ($nickname === '') {
        throw new \InvalidArgumentException('Nickname is required.');
    }

    if (mb_strlen($nickname) > 64) {
        throw new \InvalidArgumentException('Nicknames must be 64 characters or fewer.');
    }

    if ($body === '') {
        throw new \InvalidArgumentException('Comment body is required.');
    }

    if (mb_strlen($body) > 240) {
        throw new \InvalidArgumentException('Comments must be 240 characters or fewer.');
    }

    $statement = $pdo->prepare(
        'INSERT INTO comments (message_id, nickname, body) VALUES (:message_id, :nickname, :body)'
    );

    try {
        $statement->execute([
            ':message_id' => $messageId,
            ':nickname' => $nickname,
            ':body' => $body,
        ]);
    } catch (\PDOException $exception) {
        $errorCode = $exception->errorInfo[1] ?? null;

        if ((string) $exception->getCode() === '23000' && (int) $errorCode === 1452) {
            throw new \InvalidArgumentException('Unable to find the selected message for commenting.', 0, $exception);
        }

        throw new \RuntimeException('Failed to save comment.', 0, $exception);
    }

    $commentId = (int) $pdo->lastInsertId();

    $select = $pdo->prepare(
        'SELECT id, message_id, nickname, body, created_at FROM comments WHERE id = :id'
    );
    $select->execute([':id' => $commentId]);

    $comment = $select->fetch();
    if ($comment === false) {
        throw new \RuntimeException('Failed to load newly created comment.');
    }

    return $comment;
}
