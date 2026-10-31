from database.connection import get_connection

conn = get_connection()
cur = conn.cursor()

cur.execute("""
SELECT
    COLUMN_NAME,
    DATA_TYPE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME = 'jam_events'
ORDER BY ORDINAL_POSITION
""")

print("\n========== jam_events COLUMNS ==========\n")

for row in cur.fetchall():
    print(row)

conn.close()