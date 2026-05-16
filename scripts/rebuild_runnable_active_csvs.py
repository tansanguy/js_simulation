import csv
import os
import shutil
from datetime import datetime

# Source Files
SOURCES = {
    "current_main_12": "result/old/phase_next_top50_tiered_recovery_audit/top50_tiered_recovery_audit.csv",
    "signal_fix_9": "result/phase_next_top50_max_installation_recovery_20260516_002620/signal_fix_candidate_table.csv",
    "generated_signal_7": "result/phase_next_top50_max_installation_recovery_20260516_002620/generated_signal_candidate_table.csv",
    "p1_p4_recovery_6": "result/phase_next_p1_p4_recovery_6_exclude_node5830_20260516_025057/csv/p1_p4_recovery_6_candidate_table.csv"
}

# Required Counts
REQUIRED_COUNTS = {
    "current_main_12": 12,
    "signal_fix_9": 9,
    "generated_signal_7": 7,
    "p1_p4_recovery_6": 6
}

# IDs for current_main_12 (from source file matching the 12 rows in thin CSV)
CURRENT_MAIN_12_IDS = [
    "NODE_10335", "NODE_8369", "NODE_167173", "LINK_239754",
    "NODE_5846", "NODE_5831", "NODE_10377", "NODE_10376",
    "NODE_150723", "NODE_125895", "NODE_10381", "LINK_120139"
]

# Required Columns
REQUIRED_COLS = [
    "crosswalk_id", "source_crosswalk_id", "canonical_crosswalk_id", "nearest_junction_id",
    "tls_id_used", "crossing_id", "crossing_edge_id", "ped_link_index", "ped_link_indices",
    "ped_depart_offset_sec", "ped_repeat_count", "ped_repeat_spacing_sec", "route_from_edge",
    "route_to_edge", "source_file", "batch_network_file", "final_verdict", "step_test_ok",
    "controlled_links_count", "crossing_inventory_used"
]

BLOCKER_ID = "NODE_5830"

def rebuild():
    all_rows = []
    
    for group, src_path in SOURCES.items():
        print(f"Processing group: {group} from {src_path}")
        if not os.path.exists(src_path):
            print(f"Error: Source file {src_path} not found.")
            continue
            
        with open(src_path, mode='r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            src_rows = list(reader)
            
        group_rows = []
        if group == "current_main_12":
            # Filter by specific IDs and maintain order
            id_to_row = {r['crosswalk_id']: r for r in src_rows}
            for cid in CURRENT_MAIN_12_IDS:
                if cid in id_to_row:
                    row = id_to_row[cid]
                    # Map missing columns for current_main_12 source
                    clean_row = {}
                    for col in REQUIRED_COLS:
                        val = row.get(col, "")
                        if not val and col in ["ped_repeat_count"]: val = "5"
                        if not val and col in ["ped_repeat_spacing_sec"]: val = "2"
                        if not val and col in ["ped_depart_offset_sec"]: val = "0"
                        if not val and col == "source_file": val = src_path
                        if not val and col == "batch_network_file": val = "result/active/nets/current_main_12.net.xml"
                        if not val and col == "tls_id_used": val = row.get("tls_id", "")
                        clean_row[col] = val
                    group_rows.append(clean_row)
                else:
                    print(f"Warning: ID {cid} not found in {src_path}")
        else:
            for row in src_rows:
                if row.get('crosswalk_id') == BLOCKER_ID:
                    print(f"Skipping forbidden ID: {BLOCKER_ID} in {group}")
                    continue
                clean_row = {col: row.get(col, "") for col in REQUIRED_COLS}
                # Fallback for tls_id_used
                if not clean_row["tls_id_used"]:
                    clean_row["tls_id_used"] = row.get("tls_id", "")
                group_rows.append(clean_row)
        
        # Validate count
        expected = REQUIRED_COUNTS[group]
        if len(group_rows) != expected:
            print(f"Error: Row count mismatch for {group}. Expected {expected}, got {len(group_rows)}")
        
        # Write output
        out_path = f"result/active/csv/{group}_candidates.csv"
        with open(out_path, mode='w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=REQUIRED_COLS)
            writer.writeheader()
            writer.writerows(group_rows)
        print(f"Saved {len(group_rows)} rows to {out_path}")
        
        all_rows.extend(group_rows)

if __name__ == "__main__":
    rebuild()
