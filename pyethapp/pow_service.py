import time
import gevent
import gipc
import random
from devp2p.service import BaseService
from devp2p.app import BaseApp
from ethpow import mine, TT64M1
from ethereum.slogging import get_logger
log = get_logger('pow')
log_sub = get_logger('pow.subprocess')


class Miner(gevent.Greenlet):

    rounds = 100
    max_elapsed = 1.

    def __init__(self, mining_hash, block_number, nonce_callback):
        self.mining_hash = mining_hash
        self.block_number = block_number
        self.nonce_callback = nonce_callback;
        self.last = time.time()
        self.is_stopped = False
        super(Miner, self).__init__()

    def _run(self):
        nonce = random.randint(0, TT64M1)
        if not self.is_stopped:
            bin_nonce, mixhash = mine(self.block_number, self.mining_hash, start_nonce=nonce, rounds=self.rounds)
            self.nonce_callback(bin_nonce, mixhash, self.mining_hash)
        log_sub.debug('mining task finished', is_stopped=self.is_stopped)

    def stop(self):
        self.is_stopped = True
        self.join()


class PoWWorker(object):

    """
    communicates with the parent process using: tuple(str_cmd, dict_kargs)
    """

    def __init__(self, cpipe):
        self.cpipe = cpipe
        self.miner = None

    def send_found_nonce(self, bin_nonce, mixhash, mining_hash):
        log_sub.info('sending nonce')
        self.cpipe.put(('found_nonce', dict(bin_nonce=bin_nonce, mixhash=mixhash,
                                            mining_hash=mining_hash)))

    def recv_mine(self, mining_hash, block_number):
        "restarts the miner"
        log_sub.debug('received new mining task')
        assert isinstance(block_number, int)
        if self.miner:
            self.miner.stop()
        self.miner = Miner(mining_hash, block_number, self.send_found_nonce)
        self.miner.start()

    def run(self):
        while True:
            cmd, kargs = self.cpipe.get()
            assert isinstance(kargs, dict)
            getattr(self, 'recv_' + cmd)(**kargs)


def powworker_process(cpipe):
    "entry point in forked sub processes, setup env"
    gevent.get_hub().SYSTEM_ERROR = BaseException  # stop on any exception
    PoWWorker(cpipe).run()


# parent process defined below ##############################################3

class PoWService(BaseService):

    name = 'pow'
    default_config = dict(pow=dict(
        activated=False,
        coinbase_hex=None,
        mine_empty_blocks=True
    ))

    def __init__(self, app):
        super(PoWService, self).__init__(app)
        self.cpipe, self.ppipe = gipc.pipe(duplex=True)
        self.worker_process = gipc.start_process(
            target=powworker_process, args=(self.cpipe))
        self.app.services.chain.on_new_head_candidate_cbs.append(self.on_new_head_candidate)

    @property
    def active(self):
        return self.app.config['pow']['activated']

    def on_new_head_candidate(self, block):
        log.debug('new head candidate', block_number=block.number,
                  mining_hash=block.mining_hash.encode('hex'), activated=self.active)
        if not self.active:
            return
        if self.app.services.chain.is_syncing:
            return
        if (block.transaction_count == 0 and
                not self.app.config['pow']['mine_empty_blocks']):
            return

        log.debug('mining')
        self.ppipe.put(('mine', dict(mining_hash=block.mining_hash, block_number=block.number)))

    def recv_found_nonce(self, bin_nonce, mixhash, mining_hash):
        log.info('nonce found', mining_hash=mining_hash.encode('hex'))
        blk = self.app.services.chain.chain.head_candidate
        if blk.mining_hash != mining_hash:
            log.warn('mining_hash does not match')
            self.mine_head_candidate()
            return
        blk.mixhash = mixhash
        blk.nonce = bin_nonce
        self.app.services.chain.add_mined_block(blk)

    def mine_head_candidate(self):
        self.on_new_head_candidate(self.app.services.chain.chain.head_candidate)

    def _run(self):
        self.mine_head_candidate()
        while True:
            cmd, kargs = self.ppipe.get()
            assert isinstance(kargs, dict)
            getattr(self, 'recv_' + cmd)(**kargs)

    def stop(self):
        self.worker_process.terminate()
        self.worker_process.join()
        super(PoWService, self).stop()

if __name__ == "__main__":
    app = BaseApp()
    PoWService.register_with_app(app)
    app.start()
