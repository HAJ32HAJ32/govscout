CREATE TEMP TABLE migration_008_precondition (
    valid INTEGER NOT NULL CHECK (valid = 1)
);

INSERT INTO migration_008_precondition (valid)
SELECT NOT EXISTS (
    SELECT 1
    FROM fca_firms f
    JOIN leads l ON l.id = f.lead_id
    WHERE f.company_number IS NOT l.company_number
);

INSERT INTO migration_008_precondition (valid)
SELECT NOT EXISTS (
    SELECT 1
    FROM fca_firms
    WHERE website_url IS NOT NULL AND NOT (
        substr(website_url, 1, 8) = 'https://'
        AND instr(substr(website_url, 9), '/') > 1
        AND substr(
            substr(website_url, 9), 1, instr(substr(website_url, 9), '/') - 1
        ) = lower(substr(
            substr(website_url, 9), 1, instr(substr(website_url, 9), '/') - 1
        ))
        AND substr(
            substr(website_url, 9), 1, instr(substr(website_url, 9), '/') - 1
        ) NOT GLOB '*[^a-z0-9.-]*'
        AND length(substr(
            substr(website_url, 9), 1, instr(substr(website_url, 9), '/') - 1
        )) <= 253
        AND NOT EXISTS (
            WITH RECURSIVE website_labels(label, rest) AS (
                SELECT '', substr(
                    substr(website_url, 9), 1,
                    instr(substr(website_url, 9), '/') - 1
                ) || '.'
                UNION ALL
                SELECT substr(rest, 1, instr(rest, '.') - 1),
                       substr(rest, instr(rest, '.') + 1)
                FROM website_labels
                WHERE rest != ''
            )
            SELECT 1 FROM website_labels WHERE length(label) > 63
        )
        AND substr(substr(website_url, 9), 1, 1) NOT IN ('.', '-')
        AND substr(
            substr(website_url, 9), instr(substr(website_url, 9), '/') - 1, 1
        ) NOT IN ('.', '-')
        AND instr(substr(
            substr(website_url, 9), 1, instr(substr(website_url, 9), '/') - 1
        ), '..') = 0
        AND instr(substr(
            substr(website_url, 9), 1, instr(substr(website_url, 9), '/') - 1
        ), '.-') = 0
        AND instr(substr(
            substr(website_url, 9), 1, instr(substr(website_url, 9), '/') - 1
        ), '-.') = 0
        AND instr(substr(website_url, 9), ':') = 0
        AND instr(substr(website_url, 9), '@') = 0
        AND instr(website_url, '?') = 0
        AND instr(website_url, '#') = 0
        AND instr(website_url, '%') = 0
        AND instr(website_url, '\') = 0
        AND instr(website_url, char(0)) = 0
        AND website_url NOT GLOB ('*[' || char(1) || '-' || char(32) || char(127) || ']*')
        AND instr(substr(website_url, 9 + instr(substr(website_url, 9), '/')), '//') = 0
        AND instr(substr(website_url, 9 + instr(substr(website_url, 9), '/')), '/./') = 0
        AND instr(substr(website_url, 9 + instr(substr(website_url, 9), '/')), '/../') = 0
        AND substr(website_url, -2) != '/.'
        AND substr(website_url, -3) != '/..'
    )
);

DROP TABLE migration_008_precondition;

CREATE TRIGGER fca_lead_company_number_match_insert
BEFORE INSERT ON fca_firms
WHEN NEW.lead_id IS NOT NULL
     AND (SELECT company_number FROM leads WHERE id = NEW.lead_id) IS NOT NEW.company_number
BEGIN
    SELECT RAISE(ABORT, 'linked FCA and lead company numbers must match');
END;

CREATE TRIGGER fca_lead_company_number_match_update
BEFORE UPDATE OF lead_id, company_number ON fca_firms
WHEN NEW.lead_id IS NOT NULL
     AND (SELECT company_number FROM leads WHERE id = NEW.lead_id) IS NOT NEW.company_number
BEGIN
    SELECT RAISE(ABORT, 'linked FCA and lead company numbers must match');
END;

CREATE TRIGGER linked_lead_company_number_match_update
BEFORE UPDATE OF company_number ON leads
WHEN EXISTS (
    SELECT 1 FROM fca_firms
    WHERE lead_id = OLD.id AND company_number IS NOT NEW.company_number
)
BEGIN
    SELECT RAISE(ABORT, 'linked FCA and lead company numbers must match');
END;

CREATE TRIGGER fca_website_canonical_insert
BEFORE INSERT ON fca_firms
WHEN NEW.website_url IS NOT NULL AND NOT (
    substr(NEW.website_url, 1, 8) = 'https://'
    AND instr(substr(NEW.website_url, 9), '/') > 1
    AND substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ) = lower(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ))
    AND substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ) NOT GLOB '*[^a-z0-9.-]*'
    AND length(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    )) <= 253
    AND NOT EXISTS (
        WITH RECURSIVE website_labels(label, rest) AS (
            SELECT '', substr(
                substr(NEW.website_url, 9), 1,
                instr(substr(NEW.website_url, 9), '/') - 1
            ) || '.'
            UNION ALL
            SELECT substr(rest, 1, instr(rest, '.') - 1),
                   substr(rest, instr(rest, '.') + 1)
            FROM website_labels
            WHERE rest != ''
        )
        SELECT 1 FROM website_labels WHERE length(label) > 63
    )
    AND substr(substr(NEW.website_url, 9), 1, 1) NOT IN ('.', '-')
    AND substr(
        substr(NEW.website_url, 9), instr(substr(NEW.website_url, 9), '/') - 1, 1
    ) NOT IN ('.', '-')
    AND instr(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ), '..') = 0
    AND instr(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ), '.-') = 0
    AND instr(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ), '-.') = 0
    AND instr(substr(NEW.website_url, 9), ':') = 0
    AND instr(substr(NEW.website_url, 9), '@') = 0
    AND instr(NEW.website_url, '?') = 0
    AND instr(NEW.website_url, '#') = 0
    AND instr(NEW.website_url, '%') = 0
    AND instr(NEW.website_url, '\') = 0
    AND instr(NEW.website_url, char(0)) = 0
    AND NEW.website_url NOT GLOB ('*[' || char(1) || '-' || char(32) || char(127) || ']*')
    AND instr(substr(NEW.website_url, 9 + instr(substr(NEW.website_url, 9), '/')), '//') = 0
    AND instr(substr(NEW.website_url, 9 + instr(substr(NEW.website_url, 9), '/')), '/./') = 0
    AND instr(substr(NEW.website_url, 9 + instr(substr(NEW.website_url, 9), '/')), '/../') = 0
    AND substr(NEW.website_url, -2) != '/.'
    AND substr(NEW.website_url, -3) != '/..'
)
BEGIN
    SELECT RAISE(ABORT, 'FCA website URL must be canonical HTTPS without controls');
END;

CREATE TRIGGER fca_website_canonical_update
BEFORE UPDATE OF website_url ON fca_firms
WHEN NEW.website_url IS NOT NULL AND NOT (
    substr(NEW.website_url, 1, 8) = 'https://'
    AND instr(substr(NEW.website_url, 9), '/') > 1
    AND substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ) = lower(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ))
    AND substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ) NOT GLOB '*[^a-z0-9.-]*'
    AND length(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    )) <= 253
    AND NOT EXISTS (
        WITH RECURSIVE website_labels(label, rest) AS (
            SELECT '', substr(
                substr(NEW.website_url, 9), 1,
                instr(substr(NEW.website_url, 9), '/') - 1
            ) || '.'
            UNION ALL
            SELECT substr(rest, 1, instr(rest, '.') - 1),
                   substr(rest, instr(rest, '.') + 1)
            FROM website_labels
            WHERE rest != ''
        )
        SELECT 1 FROM website_labels WHERE length(label) > 63
    )
    AND substr(substr(NEW.website_url, 9), 1, 1) NOT IN ('.', '-')
    AND substr(
        substr(NEW.website_url, 9), instr(substr(NEW.website_url, 9), '/') - 1, 1
    ) NOT IN ('.', '-')
    AND instr(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ), '..') = 0
    AND instr(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ), '.-') = 0
    AND instr(substr(
        substr(NEW.website_url, 9), 1, instr(substr(NEW.website_url, 9), '/') - 1
    ), '-.') = 0
    AND instr(substr(NEW.website_url, 9), ':') = 0
    AND instr(substr(NEW.website_url, 9), '@') = 0
    AND instr(NEW.website_url, '?') = 0
    AND instr(NEW.website_url, '#') = 0
    AND instr(NEW.website_url, '%') = 0
    AND instr(NEW.website_url, '\') = 0
    AND instr(NEW.website_url, char(0)) = 0
    AND NEW.website_url NOT GLOB ('*[' || char(1) || '-' || char(32) || char(127) || ']*')
    AND instr(substr(NEW.website_url, 9 + instr(substr(NEW.website_url, 9), '/')), '//') = 0
    AND instr(substr(NEW.website_url, 9 + instr(substr(NEW.website_url, 9), '/')), '/./') = 0
    AND instr(substr(NEW.website_url, 9 + instr(substr(NEW.website_url, 9), '/')), '/../') = 0
    AND substr(NEW.website_url, -2) != '/.'
    AND substr(NEW.website_url, -3) != '/..'
)
BEGIN
    SELECT RAISE(ABORT, 'FCA website URL must be canonical HTTPS without controls');
END;
