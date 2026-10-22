-- Deterministic assignment, and the balance check that makes it usable.
--
-- Why a hash and not a random number: an experiment has to be reproducible. If assignment
-- depends on when a row was processed, re-running the analysis gives a different split and
-- two people comparing results are looking at different experiments. A hash of the unit key
-- gives the same bucket every time, on every machine, with no stored assignment table.
--
-- Why the balance check matters more than the assignment: a hash can be deterministic and
-- still be skewed on a small sample. The check is what tells you whether the split is
-- usable, and it is the part people skip.

-- 1. Bucket every book into one of ten buckets from a stable hash of its key.
--    md5 is used because it is available everywhere and stable across engines; the modulo
--    is the only place the bucket count appears, so changing it is one edit.
SELECT
    upc,
    abs(hash(upc)) % 10                                   AS bucket,
    CASE WHEN abs(hash(upc)) % 10 < 5 THEN 'control' ELSE 'treatment' END AS arm,
    price,
    stock_count,
    rating
FROM gold.dim_book;

-- 2. Balance check: the arms must not differ in a way that would explain the outcome.
--    Read this before reading any result. A difference here means the split, not the
--    treatment, produced the effect.
SELECT
    arm,
    count(*)              AS units,
    round(avg(price), 3)  AS avg_price,
    round(avg(stock_count), 3) AS avg_stock,
    round(avg(rating), 3) AS avg_rating
FROM (
    SELECT
        CASE WHEN abs(hash(upc)) % 10 < 5 THEN 'control' ELSE 'treatment' END AS arm,
        price, stock_count, rating
    FROM gold.dim_book
)
GROUP BY arm
ORDER BY arm;

-- 3. Balance ratio. With two arms and a metric that should not differ, anything far from
--    1.0 means the assignment is confounded with the metric. The threshold is a judgement
--    call and belongs in the report, not hidden in a query.
SELECT
    max(CASE WHEN arm = 'treatment' THEN avg_value END) /
    nullif(max(CASE WHEN arm = 'control' THEN avg_value END), 0) AS treatment_over_control
FROM (
    SELECT arm, avg(price) AS avg_value
    FROM (
        SELECT
            CASE WHEN abs(hash(upc)) % 10 < 5 THEN 'control' ELSE 'treatment' END AS arm,
            price
        FROM gold.dim_book
    )
    GROUP BY arm
);
