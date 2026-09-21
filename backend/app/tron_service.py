"""
TRON Nile Testnet ONLY — never Mainnet. TRON_NETWORK in config.py is
hardcoded (not env-driven) specifically so this can never accidentally
point at Mainnet from a misconfigured environment variable.

No private key is ever used to SEND funds anywhere in this file — deposit
addresses are generated locally and only ever WATCHED for incoming
transfers. There is no withdrawal/send capability in this project at all
(virtual balances only, per spec — nothing here can move real funds out).

private_key_hex is stored in plaintext in the database. This is the same
tradeoff every prior test-environment build in this codebase family has
made explicitly: acceptable ONLY because this is Nile testnet (the TRX/USDT
held has no real value) and ONLY because that key is never used to sign
anything. Do not reuse this pattern for a Mainnet deployment.
"""
import httpx
from tronpy.keys import PrivateKey

from .config import TRON_NILE_API_BASE, TRON_API_KEY, USDT_TRC20_CONTRACT_NILE


class TronServiceError(Exception):
    pass


def generate_deposit_address() -> dict:
    """Generates a brand-new, random TRON keypair — NOT derived from any
    shared seed. One throwaway key per user, deposit-only."""
    pk = PrivateKey.random()
    address = pk.public_key.to_base58check_address()
    return {"address": address, "private_key_hex": pk.hex()}


def _headers():
    h = {"Accept": "application/json"}
    if TRON_API_KEY:
        h["TRON-PRO-API-KEY"] = TRON_API_KEY
    return h


def fetch_trc20_transfers_to(address: str, limit: int = 50) -> list:
    """Real incoming TRC20 transfer events to `address`, newest first, via
    TronGrid's documented REST API. Filters to the configured Nile USDT
    contract only (see config.USDT_TRC20_CONTRACT_NILE) — a transfer of any
    other token is never even considered, regardless of what it claims to be.
    Returns a normalized list of dicts:
      {tx_hash, from_address, to_address, amount, contract_address, block_timestamp}
    amount is already converted from raw base units using USDT_TRC20_DECIMALS.
    """
    from .config import USDT_TRC20_DECIMALS

    url = f"{TRON_NILE_API_BASE}/v1/accounts/{address}/transactions/trc20"
    params = {
        "only_to": "true",
        "limit": limit,
        "contract_address": USDT_TRC20_CONTRACT_NILE,
        "order_by": "block_timestamp,desc",
    }
    try:
        resp = httpx.get(url, params=params, headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise TronServiceError(f"Could not reach TronGrid (Nile): {e}")

    out = []
    for item in data.get("data", []):
        try:
            raw_value = int(item.get("value", "0"))
            amount = raw_value / (10 ** USDT_TRC20_DECIMALS)
            out.append({
                "tx_hash": item.get("transaction_id"),
                "from_address": item.get("from"),
                "to_address": item.get("to"),
                "amount": amount,
                "contract_address": item.get("token_info", {}).get("address", USDT_TRC20_CONTRACT_NILE),
                "block_timestamp": item.get("block_timestamp"),
            })
        except Exception:
            continue  # malformed entry from the API — skip rather than crash the whole poll
    return out


def get_current_block_number() -> int:
    url = f"{TRON_NILE_API_BASE}/wallet/getnowblock"
    try:
        resp = httpx.post(url, headers=_headers(), timeout=15, json={})
        resp.raise_for_status()
        data = resp.json()
        return int(data["block_header"]["raw_data"]["number"])
    except Exception as e:
        raise TronServiceError(f"Could not fetch current Nile block height: {e}")


def get_transaction_block_number(tx_hash: str) -> int:
    """Block number a given transaction was included in — used to compute
    confirmations = current_block - tx_block."""
    url = f"{TRON_NILE_API_BASE}/wallet/gettransactioninfobyid"
    try:
        resp = httpx.post(url, headers=_headers(), timeout=15, json={"value": tx_hash})
        resp.raise_for_status()
        data = resp.json()
        block = data.get("blockNumber")
        if block is None:
            raise TronServiceError("Transaction not yet included in a block")
        return int(block)
    except TronServiceError:
        raise
    except Exception as e:
        raise TronServiceError(f"Could not fetch transaction info for {tx_hash}: {e}")
