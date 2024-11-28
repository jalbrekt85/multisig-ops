import pandas as pd
import glob
import os
import argparse
from pathlib import Path
from bal_addresses.utils import to_checksum_address
import requests
from vote import (
    prepare_vote_data,
    create_vote_payload,
    hash_eip712_message,
    _get_prop_and_determine_date_range,
)


def find_project_root(current_path=None):
    anchor_file = "multisigs.md"
    if current_path is None:
        current_path = Path(__file__).resolve().parent
    if (current_path / anchor_file).exists():
        return current_path
    parent = current_path.parent
    if parent == current_path:
        raise FileNotFoundError("Project root not found")
    return find_project_root(parent)


def fetch_gauge_labels():
    GAUGE_MAPPING_URL = "https://raw.githubusercontent.com/aurafinance/aura-contracts/main/tasks/snapshot/gauge_choices.json"
    response = requests.get(GAUGE_MAPPING_URL)
    response.raise_for_status()
    gauge_data = response.json()
    return {to_checksum_address(x["address"]): x["label"] for x in gauge_data}


def review_votes(week_string):
    year, week = week_string.split("-")
    project_root = find_project_root()
    base_path = project_root / "MaxiOps/vlaura_voting"
    voting_dir = base_path / str(year) / str(week)
    input_dir = voting_dir / "input"

    csv_files = glob.glob(str(input_dir / "*.csv"))
    if not csv_files:
        return "No CSV files found in the input directory."

    csv_file = csv_files[0]
    vote_df = pd.read_csv(csv_file)

    vote_df = vote_df.dropna(subset=["Gauge Address", "Label", "Allocation %"])

    gauge_labels = fetch_gauge_labels()

    vote_df["Checksum Address"] = vote_df["Gauge Address"].apply(
        lambda x: to_checksum_address(x.strip())
    )
    vote_df["Snapshot Label"] = vote_df["Checksum Address"].map(gauge_labels)
    missing_labels = vote_df[vote_df["Snapshot Label"].isna()]
    snapshot_label_check = len(missing_labels) == 0

    total_allocation = vote_df["Allocation %"].str.rstrip("%").astype(float).sum()
    allocation_check = abs(total_allocation - 100) < 0.0001

    # Simulate vote preparation
    try:
        prop, _, _ = _get_prop_and_determine_date_range()
        vote_df, vote_choices = prepare_vote_data(vote_df, prop)
        data = create_vote_payload(vote_choices, prop)
        hash = hash_eip712_message(data)
        vote_simulation = f"\n### Vote Simulation\nSuccessfully simulated vote preparation.\nMessage hash: `0x{hash.hex()}`"
        vote_check = True
    except Exception as e:
        vote_simulation = f"\n### Vote Simulation\n❌ Error simulating vote: {str(e)}"
        vote_check = False

    report = f"""## vLAURA Votes Review

CSV file: `{os.path.relpath(csv_file, project_root)}`

### Allocation Check
- Total allocation: {total_allocation:.2f}%
- Passes 100% check: {"✅" if allocation_check else "❌"}

### Snapshot Votes Check
- All gauge addresses have corresponding snapshot choices: {"✅" if snapshot_label_check else "❌"}
{f"- Missing labels for {len(missing_labels)} gauge(s):" if not snapshot_label_check else ""}
{missing_labels[["Chain", "Label", "Gauge Address"]].to_string(index=False) if not snapshot_label_check else ""}

{vote_simulation}

### Vote Summary

{vote_df[["Chain", "Label", "Gauge Address", "Allocation %"]].to_markdown(index=False)}

{"### ✅ All checks passed! Ready to vote!" if (allocation_check and snapshot_label_check and vote_check) else "### ❌ Some checks failed - please review the issues above"}
    """

    with open("review_output.md", "w") as f:
        f.write(report)

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vote review script")
    parser.add_argument(
        "--week-string",
        type=str,
        required=True,
        help="Date that votes are being reviewed. Should be YYYY-W##",
    )
    args = parser.parse_args()
    review_votes(args.week_string)
