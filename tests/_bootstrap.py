"""
Shared test bootstrap: wires up the offline genlayer SDK stub and loads
the contract once.

Set FORESIGHTVAULT_CONTRACT=contract_deploy.py to run the whole suite
against the comment-stripped deploy build instead of contract.py.
"""
import importlib.util
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STUB_DIR = os.path.join(_THIS_DIR, "genlayer_stub")
if _STUB_DIR not in sys.path:
    sys.path.insert(0, _STUB_DIR)

CONTRACT_FILE = os.environ.get("FORESIGHTVAULT_CONTRACT", "contract.py")
_CONTRACT_PATH = os.path.join(os.path.dirname(_THIS_DIR), CONTRACT_FILE)
_spec = importlib.util.spec_from_file_location("foresightvault_contract", _CONTRACT_PATH)
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

ForesightVault = M.ForesightVault
gl = M.gl
Address = M.Address
u256 = M.u256

import genlayer as _genlayer_stub  # noqa: E402

ConsensusFailure = _genlayer_stub.ConsensusFailure


def make_contract() -> "ForesightVault":
    return ForesightVault()


CREATOR_ADDRESS = "0x" + "10" * 20
ALICE_ADDRESS = "0x" + "11" * 20
BOB_ADDRESS = "0x" + "22" * 20
CAROL_ADDRESS = "0x" + "33" * 20
STRANGER_ADDRESS = "0x" + "44" * 20


def set_caller(address_str: str):
    """Simulate a specific wallet calling the next contract method."""
    gl.message.sender_address = Address(address_str)


def reset_transfers():
    """Clear the offline emit_transfer ledger (call in setUp)."""
    gl.evm.transfers.clear()


def call_payable(contract, method_name: str, value: int, *args, **kwargs):
    """Invoke a payable method as if `value` wei were attached. The value is
    added to contract.balance before the call and rolled back if the call
    raises, mirroring an atomic transaction."""
    gl.message.value = u256(value)
    contract.balance = contract.balance + u256(value)
    try:
        return getattr(contract, method_name)(*args, **kwargs)
    except Exception:
        contract.balance = contract.balance - u256(value)
        raise
    finally:
        gl.message.value = u256(0)


def call(contract, method_name: str, *args, **kwargs):
    """Invoke a non-payable method with zero attached value."""
    gl.message.value = u256(0)
    return getattr(contract, method_name)(*args, **kwargs)
