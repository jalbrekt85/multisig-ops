import json
import os
import time
import argparse
from functools import lru_cache
from enum import IntEnum
from pytest import approx
from dotenv import load_dotenv

import requests
from bal_addresses import AddrBook
from web3 import Web3
from eth_account._utils.structured_data.hashing import hash_message, hash_domain
from eth_utils import keccak
import pandas as pd
from web3 import Web3
from gnosis.safe import Safe
from gnosis.eth import EthereumClient
from gnosis.safe.api import TransactionServiceApi
from eth_abi import encode
from pathlib import Path
import glob

from gen_vlaura_votes_for_epoch import _get_prop_and_determine_date_range


load_dotenv()

ETHNODEURL = os.getenv("ETHNODEURL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

SAFE_API_URL = "https://safe-transaction-mainnet.safe.global"
GAUGE_MAPPING_URL = "https://raw.githubusercontent.com/aurafinance/aura-contracts/main/tasks/snapshot/gauge_choices.json"
GAUGE_SNAPSHOT_URL = "https://raw.githubusercontent.com/aurafinance/aura-contracts/main/tasks/snapshot/gauge_snapshot.json"

flatbook = AddrBook("mainnet").flatbook
vlaura_safe_addr = flatbook["multisigs/vote_incentive_recycling"]
sign_msg_lib_addr = flatbook["gnosis/sign_message_lib"]

pool_types = ["core", "sustainable", "bd"]


class Operation(IntEnum):
    CALL = 0
    DELEGATE_CALL = 1
    CREATE = 2


def post_safe_tx(safe_address, to_address, value, data, operation):
    ethereum_client = EthereumClient(ETHNODEURL)
    safe = Safe(safe_address, ethereum_client)
    safe_service = TransactionServiceApi(1, ethereum_client, SAFE_API_URL)

    safe_tx = safe.build_multisig_tx(to_address, value, data, operation)
    safe_tx.sign(PRIVATE_KEY)

    safe_service.post_transaction(safe_tx)


@lru_cache(maxsize=None)
def fetch_json_from_url(url):
    # Disable IPv6 to avoid related issues
    requests.packages.urllib3.util.connection.HAS_IPV6 = False
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def hash_eip712_message(structured_data):
    domain_hash = hash_domain(structured_data)
    message_hash = hash_message(structured_data)
    return keccak(b"\x19\x01" + domain_hash + message_hash)


def format_choices(choices):
    # custom formatting so it can be properly parsed by the snapshot
    formatted_string = '{'
    for key, value in choices.items():
        formatted_string += f'\"{key}\":{value},'
        if key == list(choices.keys())[-1]:
            formatted_string = formatted_string[:-1]
    formatted_string += '}'
    return formatted_string


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vote processing script")
    parser.add_argument(
        "--week-string",
        type=str,
        help="Date that votes are are being posted. should be YYYY-MM-DD",
        required=True,
    )

    year, week = parser.parse_args().week_string.split("-")

    project_root = Path.cwd()
    voting_dir = project_root / "MaxiOps/vlaura_voting" / str(year) / str(week)
    input_dir = voting_dir / "input"
    output_dir = voting_dir / "output"

    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    prop, start, end = _get_prop_and_determine_date_range()
    choices = prop["choices"]

    try:
        vote_df = pd.read_csv(glob.glob(f"{input_dir}/*.csv")[0])
    except:
        raise Exception(f"No input file found in {input_dir}")

    gauge_labels = fetch_json_from_url(GAUGE_MAPPING_URL)
    gauge_labels = {Web3.to_checksum_address(x["address"]): x["label"] for x in gauge_labels}
    choice_index_map = {c: x+1 for x, c in enumerate(choices)}

    vote_df["snapshot_label"] = vote_df["Gauge Address"].apply(
        lambda x: gauge_labels.get(Web3.to_checksum_address(x))
    )
    vote_df["snapshot_index"] = vote_df["snapshot_label"].apply(
        lambda label: str(choice_index_map[label])
    )
    vote_df["share"] = vote_df["Allocation %"] * 100
    
    assert vote_df["share"].sum() == approx(
        100, abs=0.0001
    )
    
    vote_choices = dict(zip(vote_df["snapshot_index"], vote_df["share"]))

    template_path = project_root / "tools/python/aura_snapshot_voting"
    with open(f"{template_path}/eip712_template.json", "r") as f:
        data = json.load(f)

    data["message"]["space"] = "balancerquadraticvoting.eth"
    data["message"]["timestamp"] = int(time.time())
    data["message"]["from"] = "0xdc9e3Ab081B71B1a94b79c0b0ff2271135f1c12b"
    data["message"]["proposal"] = bytes.fromhex("91aa92518fadf2b17106d08a7f5d4963fba0cb63034279cb2bc3f13ad4e07471")
    data["message"]["choice"] = format_choices({"1": 50, "3": 50})

    hash = hash_eip712_message(data)

    print(f"voting for: \n{vote_df[['Chain', 'snapshot_label', 'share']]}")
    print(f"payload: {data}")
    print(f"hash: {hash.hex()}")

    calldata = Web3.keccak(text="signMessage(bytes)")[0:4] + encode(["bytes"], [hash])
 
    post_safe_tx(
        "0xdc9e3Ab081B71B1a94b79c0b0ff2271135f1c12b", sign_msg_lib_addr, 0, calldata, Operation.DELEGATE_CALL
    )

    data["message"]["proposal"] = "0x91aa92518fadf2b17106d08a7f5d4963fba0cb63034279cb2bc3f13ad4e07471"
    data["types"].pop("EIP712Domain")
    data.pop("primaryType")

    with open(f"{output_dir}/report.txt", "w") as f:
        vote_data = dict(zip(vote_df['snapshot_label'], vote_df['share']))
        f.write(f"Voting for: {json.dumps(vote_data, indent=4)}\n\n")
        f.write(f"hash: 0x{hash.hex()}\n")
        f.write(f"relayer: https://relayer.snapshot.org/api/messages/0x{hash.hex()}")

    with open(f"{output_dir}/payload.json", "w") as f:
        json.dump(data, f, indent=4)
        
    response = requests.post(
        "https://relayer.snapshot.org/",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://snapshot.org/",
        },
        data=json.dumps(
                {
                    "address": "0xdc9e3Ab081B71B1a94b79c0b0ff2271135f1c12b",
                    "data": data,
                    "sig": "0x",
                }
            ),
        )
    