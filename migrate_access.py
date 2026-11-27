"""CLI Migration Tool: Core Data (Don't Copy).accdb -> data/sr_database.db"""
import sys
from app.accdb_importer import import_accdb_file

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python migrate_access.py "Core Data (Don\'t Copy).accdb"')
        sys.exit(1)
    rep = import_accdb_file(sys.argv[1], source_label="CLI")
    print(f"Method: {rep['method']}")
    for r in rep["tables"]:
        print(f"  [{r['status']:4s}] {r['table']:26s} -> {r['rows']:5d} rows ({r['detail']})")
    print(f"Total imported: {rep['total_rows']} rows.")
