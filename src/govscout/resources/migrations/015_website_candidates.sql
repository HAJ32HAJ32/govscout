CREATE TABLE website_candidate_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    fca_source_record_hash TEXT NOT NULL CHECK (
        length(fca_source_record_hash) = 64
        AND fca_source_record_hash NOT GLOB '*[^0-9a-f]*'
    ),
    company_verification_attempt_id INTEGER NOT NULL REFERENCES company_verification_attempts(id),
    query TEXT NOT NULL CHECK (
        length(query) BETWEEN 1 AND 500
        AND query = trim(query)
        AND instr(query, char(0)) = 0
        AND query NOT GLOB ('*[' || char(1) || '-' || char(31) || char(127) || ']*')
    ),
    searched_at TEXT NOT NULL CHECK (
        instr(searched_at, char(0)) = 0
        AND julianday(searched_at) IS NOT NULL
        AND substr(searched_at, 12, 2) BETWEEN '00' AND '23'
        AND substr(searched_at, 15, 2) BETWEEN '00' AND '59'
        AND substr(searched_at, 18, 2) BETWEEN '00' AND '59'
        AND strftime('%Y-%m-%dT%H:%M:%S', searched_at) = substr(searched_at, 1, 19)
        AND (
            searched_at GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'
            OR searched_at GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        )
    )
);

CREATE TABLE website_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER NOT NULL REFERENCES website_candidate_searches(id),
    rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 3),
    website_url TEXT NOT NULL CHECK(
        length(website_url) BETWEEN 9 AND 2048
        AND website_url = trim(website_url)
    ),
    source_url TEXT NOT NULL CHECK(
        length(source_url) BETWEEN 9 AND 2048
        AND source_url = trim(source_url)
    ),
    title TEXT NOT NULL CHECK (
        length(title) BETWEEN 1 AND 300
        AND title = trim(title)
        AND instr(title, char(0)) = 0
        AND title NOT GLOB ('*[' || char(1) || '-' || char(31) || char(127) || ']*')
    ),
    snippet TEXT NOT NULL CHECK (
        length(snippet) <= 1000
        AND snippet = trim(snippet)
        AND instr(snippet, char(0)) = 0
        AND snippet NOT GLOB ('*[' || char(1) || '-' || char(31) || char(127) || ']*')
    ),
    UNIQUE(search_id, rank),
    UNIQUE(search_id, website_url)
);

CREATE INDEX website_candidate_searches_firm_idx
ON website_candidate_searches(firm_id, id DESC);

CREATE TRIGGER website_candidate_searches_legal_insert
BEFORE INSERT ON website_candidate_searches
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM fca_firms AS firm
        JOIN company_verification_attempts AS attempt
          ON attempt.id = NEW.company_verification_attempt_id
         AND attempt.firm_id = firm.id
        WHERE firm.id = NEW.firm_id
          AND firm.is_active = 1
          AND firm.source_record_hash = NEW.fca_source_record_hash
          AND attempt.state = 'verified'
          AND attempt.company_number = firm.company_number
          AND attempt.fca_source_record_hash = firm.source_record_hash
          AND attempt.id = (
              SELECT id FROM company_verification_attempts
              WHERE firm_id = firm.id ORDER BY id DESC LIMIT 1
          )
          AND julianday(NEW.searched_at) - julianday(attempt.checked_at)
              BETWEEN 0 AND 30
          AND NOT EXISTS (
              SELECT 1 FROM firm_archive_events AS archive
              WHERE archive.id = (
                  SELECT id FROM firm_archive_events
                  WHERE firm_id = firm.id ORDER BY id DESC LIMIT 1
              ) AND archive.action = 'archive'
          )
    ) THEN RAISE(ABORT, 'website candidate provenance mismatch') END;
END;

CREATE TRIGGER website_candidates_url_guard
BEFORE INSERT ON website_candidates
WHEN NOT (
    substr(NEW.website_url, 1, 8) = 'https://'
    AND instr(substr(NEW.website_url, 9), '/') > 1
    AND substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ) = lower(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ))
    AND substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ) NOT GLOB '*[^a-z0-9.-]*'
    AND length(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    )) <= 253
    AND substr(substr(NEW.website_url, 9), 1, 1) NOT IN ('.', '-')
    AND substr(
        substr(NEW.website_url, 9),
        instr(substr(NEW.website_url, 9), '/') - 1, 1
    ) NOT IN ('.', '-')
    AND instr(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ), '..') = 0
    AND instr(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ), '.-') = 0
    AND instr(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ), '-.') = 0
    AND NOT EXISTS (
        WITH RECURSIVE labels(label, remainder) AS (
            SELECT
                substr(host || '.', 1, instr(host || '.', '.') - 1),
                substr(host || '.', instr(host || '.', '.') + 1)
            FROM (
                SELECT substr(
                    substr(NEW.website_url, 9), 1,
                    instr(substr(NEW.website_url, 9), '/') - 1
                ) AS host
            )
            UNION ALL
            SELECT
                substr(remainder, 1, instr(remainder || '.', '.') - 1),
                substr(remainder, instr(remainder || '.', '.') + 1)
            FROM labels WHERE remainder != ''
        )
        SELECT 1 FROM labels WHERE length(label) NOT BETWEEN 1 AND 63
    )
    AND instr(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ), ':') = 0
    AND instr(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ), '@') = 0
    AND instr(NEW.website_url, '?') = 0
    AND instr(NEW.website_url, '#') = 0
    AND instr(NEW.website_url, '%') = 0
    AND instr(NEW.website_url, '\') = 0
    AND instr(NEW.website_url, char(0)) = 0
    AND length(CAST(NEW.website_url AS BLOB)) = length(NEW.website_url)
    AND NEW.website_url NOT GLOB
        ('*[' || char(1) || '-' || char(32) || char(127) || ']*')
    AND instr(
        substr(
            substr(NEW.website_url, 9),
            instr(substr(NEW.website_url, 9), '/')
        ),
        '//'
    ) = 0
    AND instr(
        substr(
            substr(NEW.website_url, 9),
            instr(substr(NEW.website_url, 9), '/')
        ),
        '/./'
    ) = 0
    AND instr(
        substr(
            substr(NEW.website_url, 9),
            instr(substr(NEW.website_url, 9), '/')
        ),
        '/../'
    ) = 0
    AND substr(NEW.website_url, -2) != '/.'
    AND substr(NEW.website_url, -3) != '/..'
    AND substr(NEW.source_url, 1, 8) = 'https://'
    AND instr(substr(NEW.source_url, 9), '/') > 1
    AND substr(
        substr(NEW.source_url, 9), 1,
        instr(substr(NEW.source_url, 9), '/') - 1
    ) = lower(substr(
        substr(NEW.source_url, 9), 1,
        instr(substr(NEW.source_url, 9), '/') - 1
    ))
    AND length(substr(
        substr(NEW.source_url, 9), 1,
        instr(substr(NEW.source_url, 9), '/') - 1
    )) <= 253
    AND substr(
        substr(NEW.source_url, 9), 1,
        instr(substr(NEW.source_url, 9), '/') - 1
    ) NOT GLOB '*[^a-z0-9.-]*'
    AND substr(substr(NEW.source_url, 9), 1, 1) NOT IN ('.', '-')
    AND substr(
        substr(NEW.source_url, 9),
        instr(substr(NEW.source_url, 9), '/') - 1, 1
    ) NOT IN ('.', '-')
    AND instr(substr(
        substr(NEW.source_url, 9), 1,
        instr(substr(NEW.source_url, 9), '/') - 1
    ), '..') = 0
    AND instr(substr(
        substr(NEW.source_url, 9), 1,
        instr(substr(NEW.source_url, 9), '/') - 1
    ), '.-') = 0
    AND instr(substr(
        substr(NEW.source_url, 9), 1,
        instr(substr(NEW.source_url, 9), '/') - 1
    ), '-.') = 0
    AND NOT EXISTS (
        WITH RECURSIVE labels(label, remainder) AS (
            SELECT
                substr(host || '.', 1, instr(host || '.', '.') - 1),
                substr(host || '.', instr(host || '.', '.') + 1)
            FROM (
                SELECT substr(
                    substr(NEW.source_url, 9), 1,
                    instr(substr(NEW.source_url, 9), '/') - 1
                ) AS host
            )
            UNION ALL
            SELECT
                substr(remainder, 1, instr(remainder || '.', '.') - 1),
                substr(remainder, instr(remainder || '.', '.') + 1)
            FROM labels WHERE remainder != ''
        )
        SELECT 1 FROM labels WHERE length(label) NOT BETWEEN 1 AND 63
    )
    AND instr(substr(
        substr(NEW.source_url, 9), 1,
        instr(substr(NEW.source_url, 9), '/') - 1
    ), ':') = 0
    AND instr(substr(
        substr(NEW.source_url, 9), 1,
        instr(substr(NEW.source_url, 9), '/') - 1
    ), '@') = 0
    AND instr(NEW.source_url, '#') = 0
    AND instr(NEW.source_url, '%') = 0
    AND instr(NEW.source_url, '\') = 0
    AND instr(NEW.source_url, char(0)) = 0
    AND length(CAST(NEW.source_url AS BLOB)) = length(NEW.source_url)
    AND NEW.source_url NOT GLOB
        ('*[' || char(1) || '-' || char(32) || char(127) || ']*')
    AND instr(
        CASE WHEN instr(substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ), '?') = 0 THEN substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ) ELSE substr(
            substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), 1, instr(substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), '?') - 1
        ) END,
        '//'
    ) = 0
    AND instr(
        CASE WHEN instr(substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ), '?') = 0 THEN substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ) ELSE substr(
            substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), 1, instr(substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), '?') - 1
        ) END,
        '/./'
    ) = 0
    AND instr(
        CASE WHEN instr(substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ), '?') = 0 THEN substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ) ELSE substr(
            substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), 1, instr(substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), '?') - 1
        ) END,
        '/../'
    ) = 0
    AND (
        CASE WHEN instr(substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ), '?') = 0 THEN substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ) ELSE substr(
            substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), 1, instr(substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), '?') - 1
        ) END
    ) NOT GLOB '*/.'
    AND (
        CASE WHEN instr(substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ), '?') = 0 THEN substr(
            substr(NEW.source_url, 9),
            instr(substr(NEW.source_url, 9), '/')
        ) ELSE substr(
            substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), 1, instr(substr(
                substr(NEW.source_url, 9),
                instr(substr(NEW.source_url, 9), '/')
            ), '?') - 1
        ) END
    ) NOT GLOB '*/..'
)
BEGIN
    SELECT RAISE(ABORT, 'website candidate URLs must be canonical HTTPS');
END;

CREATE TRIGGER website_candidate_searches_no_update
BEFORE UPDATE ON website_candidate_searches BEGIN
    SELECT RAISE(ABORT, 'website candidate searches are immutable');
END;
CREATE TRIGGER website_candidate_searches_no_delete
BEFORE DELETE ON website_candidate_searches BEGIN
    SELECT RAISE(ABORT, 'website candidate searches are immutable');
END;
CREATE TRIGGER website_candidates_no_update
BEFORE UPDATE ON website_candidates BEGIN
    SELECT RAISE(ABORT, 'website candidates are immutable');
END;
CREATE TRIGGER website_candidates_no_delete
BEFORE DELETE ON website_candidates BEGIN
    SELECT RAISE(ABORT, 'website candidates are immutable');
END;
