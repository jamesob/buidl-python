from buidl.hd import get_unhardened_child_path, HDPublicKey
from buidl.psbt import MixedNetwork, NamedHDPublicKey, PSBT
from buidl.tx import Tx, TxIn, TxOut
from buidl.script import RedeemScript, address_to_script_pubkey


def _append(mydict, key, val):
    # append a key/val to a dict
    if key in mydict:
        mydict[key].append(val)
    else:
        mydict[key] = [val]


def _get_child_hdpubkey(xpub_dicts, root_path):
    """
    Iterate through a list of
    """
    for xpub_dict in xpub_dicts:
        child_path = get_unhardened_child_path(
            root_path=root_path,
            base_path=xpub_dict["base_path"],
        )
        if child_path:
            return xpub_dict["xpub_obj"].traverse(child_path)


def create_ps2sh_multisig_psbt(
    xpubs_dict,
    input_dicts,
    output_dicts,
    fee_sats,
):
    """
    Helper method to create a p2sh multisig PSBT.

    network (testnet/mainnet) will be inferred from xpubs/tpubs.
    """

    xfp_dict, tx_lookup, pubkey_lookup, redeem_lookup = {}, {}, {}, {}
    network = None

    # This at the child pubkey lookup that each input will traverse off of
    for xfp_hex, base_paths in xpubs_dict.items():
        for base_path in base_paths:

            hd_pubkey_obj = HDPublicKey.parse(base_path["xpub_hex"])

            # We will use this dict/list structure for each input/ouput in the for-lopps below
            _append(
                xfp_dict,
                key=xfp_hex,
                val={
                    "xpub_obj": hd_pubkey_obj,
                    "base_path": base_path["base_path"],
                },
            )

            if network is None:
                # Set the initial value
                network = hd_pubkey_obj.network
            else:
                # Confirm it hasn't changed
                if network != hd_pubkey_obj.network:
                    raise MixedNetwork(f"Mixed networks in xpubs: {xpubs_dict}")

    tx_ins = []
    for cnt, input_dict in enumerate(input_dicts):

        # Prev tx stuff
        prev_tx_dict = input_dict["prev_tx_dict"]
        prev_tx_obj = Tx.parse_hex(prev_tx_dict["hex"])
        tx_lookup[prev_tx_obj.hash()] = prev_tx_obj

        if prev_tx_dict["hash_hex"] != prev_tx_obj.hash().hex():
            raise ValueError(
                f"Hash digest mismatch for input #{cnt}: {prev_tx_dict['hash_hex']} != {prev_tx_obj.hash().hex()}"
            )

        # pubkey lookups needed for validation
        input_pubkey_hexes, total_input_sats = [], 0
        for xfp_hex, root_path in input_dict["path_dict"].items():
            # Get the correct xpub/path
            child_hd_pubkey = _get_child_hdpubkey(
                xpub_dicts=xfp_dict.get(xfp_hex, []),
                root_path=root_path,
            )
            if child_hd_pubkey is None:
                raise ValueError(
                    f"xfp_hex {xfp_hex} from input #{cnt} not supplied in xpubs_dict:  {xpubs_dict}"
                )
            input_pubkey_hexes.append(child_hd_pubkey.sec().hex())

            # Enhance the PSBT
            named_hd_pubkey_obj = NamedHDPublicKey.from_hd_pub(
                child_hd_pub=child_hd_pubkey,
                fingerprint_hex=xfp_hex,
                root_path=root_path,
            )
            pubkey_lookup[named_hd_pubkey_obj.sec()] = named_hd_pubkey_obj

        utxo = prev_tx_obj.tx_outs[prev_tx_dict["output_idx"]]

        # Grab amount as developer safety check
        if prev_tx_dict["output_sats"] != utxo.amount:
            raise ValueError(
                f"Wrong number of sats for input #{cnt}! Expecting {prev_tx_dict['output_sats']} but got {utxo.amount}"
            )
        total_input_sats += utxo.amount

        # This allows us to spend from a redeem script with pubkeys out of order
        redeem_script = RedeemScript.create_p2sh_multisig_unsorted(
            quorum_m=input_dict["quorum_m"],
            pubkey_hex_list=input_pubkey_hexes,
            target_address=utxo.script_pubkey.address(network=network),
            network=network,
        )

        # Confirm address matches previous ouput
        if redeem_script.address(network=network) != utxo.script_pubkey.address(
            network=network
        ):
            raise ValueError(
                f"Invalid redeem script for input #{cnt}. Expecting {redeem_script.address(network=network)} but got {utxo.script_pubkey.address(network=network)}"
            )

        tx_in = TxIn(prev_tx=prev_tx_obj.hash(), prev_index=prev_tx_dict["output_idx"])
        tx_ins.append(tx_in)

        # For enhancing the PSBT for HWWs:
        redeem_lookup[redeem_script.hash160()] = redeem_script

    tx_outs = []
    for output_dict in output_dicts:
        tx_out = TxOut(
            amount=output_dict["sats"],
            script_pubkey=address_to_script_pubkey(output_dict["address"]),
        )
        tx_outs.append(tx_out)

        if output_dict.get("path_dict"):
            # This output claims to be change, so we must validate it here
            output_pubkey_hexes = []
            for xfp_hex, root_path in output_dict["path_dict"].items():
                child_hd_pubkey = _get_child_hdpubkey(
                    xpub_dicts=xfp_dict.get(xfp_hex, []),
                    root_path=root_path,
                )
                if child_hd_pubkey is None:
                    raise ValueError(
                        f"xfp_hex {xfp_hex} from output #{cnt} not supplied in xpubs_dict: {xpubs_dict}"
                    )
                output_pubkey_hexes.append(child_hd_pubkey.sec().hex())

                # Enhance the PSBT
                named_hd_pubkey_obj = NamedHDPublicKey.from_hd_pub(
                    child_hd_pub=child_hd_pubkey,
                    fingerprint_hex=xfp_hex,
                    root_path=root_path,
                )
                pubkey_lookup[named_hd_pubkey_obj.sec()] = named_hd_pubkey_obj

            redeem_script = RedeemScript.create_p2sh_multisig(
                quorum_m=output_dict["quorum_m"],
                pubkey_hex_list=output_pubkey_hexes,
                # We intentionally only allow change addresses (output addresses) to be lexicographically sorted
                sort_keys=True,
            )
            # Confirm address matches previous ouput
            if redeem_script.address(network=network) != output_dict["address"]:
                raise ValueError(
                    f"Invalid redeem script for output #{cnt}. Expecting {redeem_script.address(network=network)} but got {output_dict['address']}"
                )

    tx_obj = Tx(
        version=1,
        tx_ins=tx_ins,
        tx_outs=tx_outs,
        locktime=0,
        network=network,
        segwit=True,
    )

    # Safety check to try and prevent footgun

    calculated_fee_sats = total_input_sats - sum([tx_out.amount for tx_out in tx_outs])
    if fee_sats != calculated_fee_sats:
        raise ValueError(
            f"TX fee of {fee_sats} sats supplied != {calculated_fee_sats} sats calculated"
        )

    psbt_obj = PSBT.create(tx_obj)
    psbt_obj.update(
        tx_lookup=tx_lookup,
        pubkey_lookup=pubkey_lookup,
        redeem_lookup=redeem_lookup,
        witness_lookup=None,
    )

    if psbt_obj.validate() is not True:
        raise ValueError(f"PSBT does not validate: {psbt_obj.serialize_base64()}")

    return psbt_obj
