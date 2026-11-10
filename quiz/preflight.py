"""Read-only legacy-data preflight for the ASSESS-02 quiz migrations.

Uses raw SQL over columns that exist both before and after the migrations, so
the ``assessment_preflight`` command and migration ``quiz.0004`` share one
definition independent of current model code. Reports record IDs only.
"""


def find_duplicate_submissions(connection):
    """``[(quiz_id, student_id, [submission ids...]), ...]`` for duplicate pairs.

    These block the one-submission-per-student/quiz constraint. Nothing is
    deleted or merged automatically.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT quiz_id, student_id FROM quiz_submission '
            'GROUP BY quiz_id, student_id HAVING COUNT(*) > 1 ORDER BY quiz_id, student_id')
        pairs = cursor.fetchall()
        report = []
        for quiz_id, student_id in pairs:
            cursor.execute('SELECT id FROM quiz_submission WHERE quiz_id = %s AND student_id = %s '
                           'ORDER BY id', [quiz_id, student_id])
            report.append((quiz_id, student_id, [row[0] for row in cursor.fetchall()]))
    return report


def format_duplicates(report):
    return [f'duplicate submissions: quiz {quiz_id} student {student_id}: submission IDs '
            + ', '.join(str(pk) for pk in ids)
            for quiz_id, student_id, ids in report]


def legacy_notes(connection):
    """Non-blocking facts about how legacy rows are carried forward (IDs only)."""
    notes = []
    with connection.cursor() as cursor:
        cursor.execute('SELECT id FROM quiz_quiz WHERE is_active ORDER BY id')
        active = [row[0] for row in cursor.fetchall()]
        if active:
            notes.append('active quizzes that become drafts pending validation (legacy_is_active '
                         'keeps the old flag): quiz IDs ' + ', '.join(map(str, active)))
        cursor.execute("SELECT id FROM quiz_question WHERE type <> 'mcq' ORDER BY id")
        unsupported = [row[0] for row in cursor.fetchall()]
        if unsupported:
            notes.append('preserved non-MCQ questions (their quizzes cannot be published): '
                         'question IDs ' + ', '.join(map(str, unsupported)))
        cursor.execute('SELECT COUNT(*) FROM quiz_submission')
        count = cursor.fetchone()[0]
        if count:
            notes.append(f'{count} existing submissions keep their score/answers and are marked '
                         'legacy (no attempt, total_points or snapshot is fabricated)')
    return notes
