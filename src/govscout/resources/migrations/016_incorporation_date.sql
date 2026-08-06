ALTER TABLE company_verification_attempts
ADD COLUMN incorporation_date TEXT CHECK (
    incorporation_date IS NULL OR (
        length(incorporation_date) = 10
        AND julianday(incorporation_date) IS NOT NULL
        AND incorporation_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    )
);
