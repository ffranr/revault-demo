import bitcoin
import bitcoin.rpc
import threading

from flask import Flask, jsonify, request, abort
from decimal import Decimal


class SigServer:
    """
    A wrapper around a dead simple server storing signatures and providing
    feerates, note that it intentionally doesn't do any checks or
    authentication. Poor API, don't mind that.
    """
    def __init__(self, bitcoind_conf_path):
        """Uncommon pattern, but a handy one. We setup everything when the
        wrapper is initialized."""
        self.server = Flask(__name__)
        # The dict storing the ordered (hex) signatures, like:
        # signatures["txid"] = [sig_stk1, sig_stk2, sig_stk3, sig_stk4]
        self.signatures = {}
        # We need to talk to bitcoind to gather feerates
        self.bitcoind_conf_path = bitcoind_conf_path
        self.bitcoind = bitcoin.rpc.RawProxy(btc_conf_file=bitcoind_conf_path)
        self.bitcoind_lock = threading.Lock()
        # We need to give the same feerate to all the wallets, so we keep track
        # of the feerate we already gave by txid
        self.feerates = {}
        # A dictionary to store each stakeholder acceptance to a spend,
        # represented as a list of four booleans.
        self.spend_acceptance = {}
        # A dictionary to store each spend destinations by txid.
        self.spend_requests = {}
        # Used to test fee bumping
        self.mocked_feerate = None
        self.setup_routes()

    def setup_routes(self):
        @self.server.route("/sig/<string:txid>/<int:stk_id>",
                           methods=["POST", "GET"])
        def get_post_signatures(txid, stk_id):
            """Get or give a signature for {txid}, by the {stk_id}th
            stakeholder."""
            if request.method == "POST":
                if txid not in self.signatures.keys():
                    self.signatures[txid] = [None] * 4
                sig = request.form.get("sig", None)
                self.signatures[txid][stk_id - 1] = sig
                return jsonify({"sig": sig}), 201
            elif request.method == "GET":
                if txid not in self.signatures:
                    abort(404)
                sig = self.signatures[txid][stk_id - 1]
                if sig is None:
                    abort(404)
                return jsonify({"sig": sig}), 200

        @self.server.route("/feerate/<string:tx_type>/<string:txid>",
                           methods=["POST", "GET"])
        def get_feerate(tx_type, txid):
            """Get the feerate for any transaction.

            We have 4 types: unvault, cancel, spend, and emergency.
            """
            if tx_type not in {"unvault", "cancel", "spend", "emergency"}:
                raise Exception("Unsupported tx type for get_feerate.")

            if txid not in self.feerates.keys():
                if self.mocked_feerate is not None:
                    feerate = self.mocked_feerate
                elif tx_type == "emergency":
                    # We use 10* the conservative estimation at 2 block for
                    # such a crucial transaction
                    feerate = self.estimatefee_hack(2, "CONSERVATIVE")
                    feerate *= Decimal(10)
                elif tx_type == "cancel":
                    # Another crucial transaction, but which is more likely to
                    # be broadcasted: a lower high feerate.
                    feerate = self.estimatefee_hack(2, "CONSERVATIVE")
                    feerate *= Decimal(5)
                else:
                    # Not a crucial transaction (spend / unvault), but don't
                    # greed!
                    feerate = self.estimatefee_hack(3, "CONSERVATIVE")
                self.feerates[txid] = feerate

            return jsonify({"feerate": float(self.feerates[txid])})

        @self.server.route("/requestspend", methods=["POST"])
        def request_spend():
            """Request to spend this vault to this address.

            This is called by the spend initiator to advertise its will.
            """
            params = request.get_json()

            txid = params["vault_txid"]
            self.spend_requests[txid] = params["addresses"]
            self.spend_acceptance[txid] = [None, None, None, None]

            return jsonify({"success": True}), 201

        @self.server.route("/acceptspend/<string:vault_txid>/<string:address>"
                           "/<int:stk_id>", methods=["POST"])
        def accept_spend(vault_txid, address, stk_id):
            """Make stakeholder n°{stk_id} accept this spend."""
            self.spend_acceptance[vault_txid][stk_id - 1] = True

            return jsonify({"success": True}), 201

        @self.server.route("/refusespend/<string:vault_txid>/<string:address>"
                           "/<int:stk_id>", methods=["POST"])
        def refuse_spend(vault_txid, address, stk_id):
            """Make stakeholder n°{stk_id} accept this spend."""
            self.spend_acceptance[vault_txid][stk_id - 1] = False

            return jsonify({"success": True}), 201

        @self.server.route("/spendaccepted/<string:vault_txid>",
                           methods=["GET"])
        def spendaccepted(vault_txid):
            """Have all stakeholder accepted this spend ?

            We use null for not completed, True for accepted, False for
            rejected.
            """
            if None in self.spend_acceptance[vault_txid]:
                return jsonify({"accepted": None})
            return jsonify({
                "accepted": all(self.spend_acceptance[vault_txid])
            })

        @self.server.route("/spendrequests", methods=["GET"])
        def spendrequests():
            return jsonify(self.spend_requests)

    def mock_feerate(self, feerate):
        """Return this feerate instead of asking bitcoind, used to test."""
        self.mocked_feerate = feerate

    def estimatefee_hack(self, target, mode):
        # FIXME, this is a hack !
        err = None
        self.bitcoind_lock.acquire()
        try:
            feerate = self.bitcoind.estimatesmartfee(target, mode)
        except Exception as e:
            err = e
        self.bitcoind_lock.release()
        if err is not None:
            raise err
        if "feerate" not in feerate:
            raise Exception("Could not estimate fees !")
        return feerate["feerate"]

    def test_client(self):
        return self.server.test_client()

    def run(self, host, port, debug):
        self.server.run(host, port, debug)
