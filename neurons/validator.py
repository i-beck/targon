from os import urandom
import json
import copy
import time
import random
import asyncio

from asyncpg.connection import asyncpg
import openai
from neurons.base import BaseNeuron, NeuronType
from targon.dataset import create_query_prompt, create_search_prompt
from targon.updater import autoupdate
from targon.utils import (
    normalize,
    print_info,
    safe_mean_score,
    InferenceStats,
    check_tokens,
)
import traceback
import math
import numpy as np
import pandas as pd
import bittensor as bt
from nanoid import generate
from datetime import datetime

from typing import Any, Dict, List, Optional, Tuple
from targon import (
    protocol,
    __version__,
    __spec_version__ as spec_version,
)
from bittensor.utils.weight_utils import (
    process_weights_for_netuid,
)


class Validator(BaseNeuron):
    miner_wps: Dict[int, Any]
    db_stats: Optional[asyncpg.Connection]
    db_organics: Optional[asyncpg.Connection]
    neuron_type = NeuronType.Validator

    def __init__(self, config=None):
        super().__init__(config)
        ## Typesafety
        assert self.config.netuid
        assert self.config.neuron
        assert self.config.axon
        assert self.config.database

        ## BITTENSOR INITIALIZATION
        self.dendrite = bt.dendrite(wallet=self.wallet)

        ## CHECK IF REGG'D
        if not self.metagraph.validator_permit[self.uid]:
            bt.logging.error("Validator does not have vpermit")
            exit()

        ## SET MISC PARAMS
        self.hotkeys = self.metagraph.hotkeys
        self.next_forward_block = None
        self.last_posted_weights = self.metagraph.last_update[self.uid]
        bt.logging.info(f"Last updated at block {self.last_posted_weights}")

        ## STATS
        self.miner_wps = {}

        try:
            with open(self.config.neuron.cache_file, "r") as file:
                loaded_data: Dict[str, Any] = json.load(file)
                # Only load cache if fresh
                if loaded_data.get("block_saved", 0) > self.subtensor.block - 360:
                    bt.logging.info("Loading cached data")
                    bt.logging.info(str(loaded_data))
                    self.miner_wps = loaded_data.get("miner_wps", {})
        except IOError:
            bt.logging.info("No cache file found")
        except EOFError:
            bt.logging.warning("Curropted pickle file")
        except Exception as e:
            bt.logging.error(f"Failed reading cache file: {e}")
            bt.logging.error(traceback.format_exc())

        miners = self.get_miner_uids()
        for miner in miners:
            if self.miner_wps.get(miner) == None:
                self.miner_wps[miner] = []

        ## SET DATASET
        self.dataset = pd.read_json(
            "hf://datasets/pinecone/dl-doc-search/train.jsonl", lines=True
        )
        bt.logging.info(
            "\N{grinning face with smiling eyes}", "Successfully Initialized!"
        )
        try:
            self.db_stats = None
            if self.config.database.url:
                self.db_stats = self.loop.run_until_complete(
                    asyncpg.connect(self.config.database.url)
                )
        except Exception as e:
            bt.logging.error(f"Failed to initialize stats database: {e}")
        try:
            self.db_organics = None
            if self.config.database.organics_url:
                self.db_organics = self.loop.run_until_complete(
                    asyncpg.connect(self.config.database.organics_url)
                )
        except Exception as e:
            bt.logging.error(f"Failed to initialize organics database: {e}")

    async def add_records(self, miners_records, response_records):
        try:
            assert self.db_stats
            # Insert miners_records
            await self.db_stats.executemany(
                """
                INSERT INTO miner_response (r_nanoid, hotkey, coldkey, uid, stats) VALUES ($1, $2, $3, $4, $5)
            """,
                miners_records,
            )
            bt.logging.info("Records inserted into miner responses successfully.")

            # Insert response_records first since miners_responses references it
            await self.db_stats.executemany(
                """
                INSERT INTO validator_request (r_nanoid, block, timestamp, sampling_params, ground_truth, version) VALUES ($1, $2, $3, $4, $5, $6)
            """,
                response_records,
            )
            bt.logging.info("Records inserted into validator request successfully.")

        except Exception as e:
            bt.logging.error(f"Error inserting records: {e}")
            bt.logging.error(traceback.format_exc())

    async def handle_inference(self, messages, sampling_params, uid, ground_truth):
        assert self.config.neuron
        stats = InferenceStats(
            time_to_first_token=0,
            time_for_all_tokens=0,
            wps=0,
            total_time=0,
            response="",
            verified=False,
            jaros=[],
        )
        try:
            synapse = protocol.Inference(
                messages=json.dumps(messages),
                sampling_params=sampling_params,
            )
            response_tokens = []
            token_count = 0
            start_send_message_time = time.time()
            end_send_message_time = None
            start_token_time = 0
            async for token in await self.dendrite(
                self.metagraph.axons[uid],
                synapse,
                deserialize=False,
                timeout=self.config.neuron.timeout,
                streaming=True,
            ):
                if token_count == 1:
                    end_send_message_time = time.time()
                    start_token_time = time.time()
                if isinstance(token, protocol.Inference):
                    continue
                response_tokens.append(token)
                token_count += 1

            if end_send_message_time is None:
                end_send_message_time = time.time()
                start_token_time = end_send_message_time
            end_token_time = time.time()
            time_to_first_token = end_send_message_time - start_send_message_time
            time_for_all_tokens = end_token_time - start_token_time
            response = "".join(response_tokens)

            jaros, verified = check_tokens(response.split(" "), ground_truth.split(" "))
            stats.jaros = jaros
            stats.verified = verified
            stats.time_to_first_token = time_to_first_token
            stats.time_for_all_tokens = time_for_all_tokens
            stats.total_time = end_token_time - start_send_message_time
            stats.response = response
            stats.wps = (
                min(len(stats.response.split(" ")), len(ground_truth.split(" ")))
                / stats.total_time
            )
            return uid, stats
        except Exception as e:
            bt.logging.error(f"Error in forward: {e}")
            bt.logging.error(traceback.format_exc())
            return uid, stats

    def save_scores(self):
        try:
            assert self.config.neuron
            with open(self.config.neuron.cache_file, "w") as file:
                bt.logging.info("Caching scores...")
                json.dump(
                    {
                        "miner_wps": self.miner_wps,
                        "block_saved": self.subtensor.block,
                        "version": spec_version,
                    },
                    file,
                )
                bt.logging.info("Cached")
        except Exception as e:
            bt.logging.error(f"Failed writing to cache file: {e}")
            bt.logging.error(traceback.format_exc())

    def generate_ground_truth(self, messages, sampling_params):
        assert self.config.neuron
        res = self.client.chat.completions.create(
            model=self.config.neuron.model_name,
            messages=messages,
            stream=False,
            temperature=sampling_params.temperature,
            top_p=sampling_params.top_p,
            seed=sampling_params.seed,
            max_tokens=sampling_params.max_new_tokens,
        )
        return res.choices[0].message.content

    async def save_stats_to_db(self, stats, sampling_params, messages, ground_truth):
        r_nanoid = generate(size=48)
        miners_records = [
            (
                r_nanoid,
                self.metagraph.axons[uid].hotkey,
                self.metagraph.axons[uid].coldkey,
                int(uid),
                json.dumps(stat.model_dump()),
            )
            for uid, stat in stats
        ]
        response_records = [
            (
                r_nanoid,
                self.subtensor.block,
                datetime.now(),
                json.dumps(sampling_params.model_dump()),
                json.dumps({"ground_truth": ground_truth, "messages": messages}),
                spec_version,
            )
        ]
        await self.add_records(miners_records, response_records)

    async def query_miners(self, miner_uids, save_to_db=True):
        assert self.config.database
        try:
            messages, sampling_params = self.generate_question()
            ground_truth = self.generate_ground_truth(messages, sampling_params)
        except openai.APIConnectionError as e:
            bt.logging.error(
                f"Failed to connect to LLM server with connection string {self.client.base_url}: {e.message}"
            )
            bt.logging.error(
                "Make sure an open ai compliant server is running at the above url, or fix --neuron.model_endpoint"
            )
            return None
        except Exception as e:
            bt.logging.error(f"Error generating dataset: {e}")
            bt.logging.error(traceback.format_exc())
            return None

        tasks = []
        for uid in miner_uids:
            tasks.append(
                asyncio.create_task(
                    self.handle_inference(messages, sampling_params, uid, ground_truth)
                )
            )
        stats: List[Tuple[int, InferenceStats]] = await asyncio.gather(*tasks)
        for uid, stat in stats:
            bt.logging.info(
                f"{uid}: {stat.verified} | {stat.total_time} | {stat.jaros}"
            )
            if stat.verified and stat.total_time != 0:
                self.miner_wps[uid].append(stat.wps)
                continue
            self.miner_wps[uid].append(None)

        if self.config.database.url and save_to_db:
            await self.save_stats_to_db(stats, sampling_params, messages, ground_truth)

        self.save_scores()
        return (
            stats,
            ground_truth,
            sampling_params,
            messages,
        )

    async def score_organic(self):
        bt.logging.info(f"Scoring up to 5 random recent organics")
        try:
            assert self.db_organics
            rows = await self.db_organics.fetch(
                f"""
SELECT response, uid, pub_id, request->'messages' as messages, request->'max_tokens' as max_tokens, metadata->'request_duration_ms' as total_time FROM organic_request
WHERE scored=FALSE AND created_at >= (NOW() - INTERVAL '30 minutes') LIMIT 5"""
            )
            bt.logging.info(f"Found {len(rows)} rows")
            for row in rows:
                try:
                    response = row["response"]
                    uid = row["uid"]
                    if self.miner_wps.get(uid) is None:
                        self.miner_wps[uid] = []
                    if response is None:
                        self.miner_wps[uid].extend([None] * 3)
                        bt.logging.info(f"Organic: {uid} failed req completely")
                        continue

                    sampling_params = protocol.InferenceSamplingParams(
                        seed=5688697,
                        temperature=0.01,
                        top_p=0.98,
                        max_new_tokens=row["max_tokens"],
                    )
                    messages = json.loads(row["messages"])
                    ground_truth = self.generate_ground_truth(messages, sampling_params)
                    if ground_truth is None:
                        continue

                    response_words = row["response"].split(" ")
                    ground_truth_words = ground_truth.split(" ")
                    jaros, verified = check_tokens(response_words, ground_truth_words)
                    stat = InferenceStats(
                        time_to_first_token=0,
                        time_for_all_tokens=0,
                        total_time=row["total_time"],
                        wps=(
                            min(len(response_words), len(ground_truth_words))
                            / float(row["total_time"])
                        ),
                        response="",
                        jaros=jaros,
                        verified=verified,
                    )
                    bt.logging.info(
                        f"Organic: {uid}: {stat.verified} | {stat.total_time}ms"
                    )
                    await self.db_organics.execute(
                        "UPDATE organic_request SET scored=True",
                    )
                    if stat.verified:
                        self.miner_wps[uid].extend([stat.wps] * 3)
                        continue
                    self.miner_wps[uid].append(stat.wps * (sum(jaros) / len(jaros)))
                except Exception as e:
                    bt.logging.error(
                        f"Error scoring organic requests for {row['uid']}: {e}"
                    )
                    bt.logging.error(traceback.format_exc())
                    continue
        except Exception as e:
            bt.logging.error(f"Error scoring organic requests: {e}")
            bt.logging.error(traceback.format_exc())

    def run(self):
        assert self.config.subtensor
        assert self.config.neuron
        assert self.config.database
        if self.sync_metagraph():
            self.resync_hotkeys()
        bt.logging.info(
            f"Running validator on network: {self.config.subtensor.chain_endpoint} with netuid: {self.config.netuid}"
        )

        bt.logging.info(f"Validator starting at block: {self.subtensor.block}")

        # This loop maintains the validator's operations until intentionally stopped.
        step = 0
        miner_subset = 36
        miner_uids = self.get_miner_uids()
        random.shuffle(miner_uids)
        miner_uids = miner_uids[:miner_subset]
        while not self.should_exit:
            bt.logging.info(
                f"Forward Block: {self.subtensor.block} | Step {step} |  Blocks till Set Weights: { self.config.neuron.epoch_length - (self.subtensor.block % self.config.neuron.epoch_length) }"
            )

            # Sync metagraph
            if self.sync_metagraph():
                self.resync_hotkeys()

            # Set weights
            if (
                self.subtensor.block % self.config.neuron.epoch_length == 0
                and self.last_posted_weights != self.subtensor.block
            ):
                self.last_posted_weights = self.subtensor.block
                self.set_weights()

                # Only keep last 15 scores
                for uid in self.miner_wps.keys():
                    self.miner_wps[uid] = self.miner_wps[uid][-15:]

            # Stop querying if close to weight set block
            if (
                self.config.neuron.epoch_length
                - (self.subtensor.block % self.config.neuron.epoch_length)
                < 5
            ):
                continue

            # Check to see if we need to update
            if self.config.autoupdate:
                autoupdate(branch="main")

            # Score organic queries every few steps
            if not step % 25 and self.config.database.organics_url:
                self.loop.run_until_complete(self.score_organic())

            print_info(
                self.metagraph,
                self.wallet.hotkey.ss58_address,
                self.subtensor.block,
                isMiner=False,
            )

            # get random set of miner uids every other step
            if step % 2:
                miner_uids = self.get_miner_uids()
                random.shuffle(miner_uids)
                miner_uids = miner_uids[:miner_subset]
            self.loop.run_until_complete(self.query_miners(miner_uids))
            step += 1

        # Exiting
        self.shutdown()

    def shutdown(self):
        if self.db_stats:
            bt.logging.info("Closing stats db connection")
            self.loop.run_until_complete(self.db_stats.close())
        if self.db_organics:
            bt.logging.info("Closing organics db connection")
            self.loop.run_until_complete(self.db_organics.close())

    def generate_question(self):
        assert self.config.neuron
        # Generate a random seed for reproducibility in sampling and text generation
        random.seed(urandom(100))
        seed = random.randint(10000, 10000000)

        # Determine the maximum number of new tokens to generate
        max_new_tokens = random.randint(1024 * 5, 1024 * 10)

        # Create sampling parameters using the generated seed and token limit
        sampling_params = protocol.InferenceSamplingParams(
            seed=seed, max_new_tokens=max_new_tokens
        )

        # Sample a random row from the dataset and extract the text
        random_row_text = self.dataset.sample(n=1)["text"].iloc[0]

        # Generate a query from the sampled text and perform text generation
        messages = create_query_prompt(random_row_text)

        # If this fails, it gets caught in the same try/catch as ground truth generation
        res = self.client.chat.completions.create(
            model=self.config.neuron.model_name,
            messages=messages,
            stream=False,
            temperature=0.5,
            top_p=sampling_params.top_p,
            seed=sampling_params.seed,
            max_tokens=random.randint(16, 64),
        )

        # Create a final search prompt using the query and sources
        completion = res.choices[0].message.content
        if completion is None:
            print(res)
            raise Exception("No completion")
        prompt = create_search_prompt(completion)

        return prompt, sampling_params

    def get_weights(self) -> Tuple[List[int], List[float]]:
        wps = {
            miner: safe_mean_score(self.miner_wps[miner][-15:])
            for miner in self.miner_wps
        }
        wps_list = list(wps.values())
        if len(wps_list) == 0:
            bt.logging.warning("Not setting weights, no responses from miners")
            return [], []
        top_wps = max(wps_list)
        range_wps = top_wps - min(wps_list)
        avg_wps = np.average(wps_list)

        rewards = {}
        for uid, s in wps.items():
            reward_multiplier = 1
            if s > 0:
                normalized_difference = (s - avg_wps) / range_wps
                reward_multiplier = math.exp(
                    normalized_difference * 10
                )  # Scale the difference to enhance reward disparity

            rewards[uid] = reward_multiplier * s if s != 0 else 1e-9
        uids: List[int] = sorted(rewards.keys())
        rewards = [rewards[uid] for uid in uids]

        bt.logging.info(f"All wps: {wps}")
        if sum(rewards) == 0:
            bt.logging.warning("No one gave responses worth scoring")
            return [], []
        raw_weights = normalize(rewards)
        bt.logging.info(f"Raw Weights: {raw_weights}")
        return uids, raw_weights

    def set_weights(self):
        assert self.config.netuid
        uids, raw_weights = self.get_weights()
        if not len(uids):
            return

        # Set the weights on chain via our subtensor connection.
        (
            processed_weight_uids,
            processed_weights,
        ) = process_weights_for_netuid(
            uids=np.asarray(uids),
            weights=np.asarray(raw_weights),
            netuid=self.config.netuid,
            subtensor=self.subtensor,
            metagraph=self.metagraph,
        )

        bt.logging.info("Setting Weights: " + str(processed_weights))
        bt.logging.info("Weight Uids: " + str(processed_weight_uids))
        result, message = self.subtensor.set_weights(
            wallet=self.wallet,
            netuid=self.config.netuid,
            uids=processed_weight_uids,  # type: ignore
            weights=processed_weights,
            wait_for_finalization=False,
            wait_for_inclusion=False,
            version_key=spec_version,
            max_retries=1,
        )
        if result is True:
            bt.logging.info("set_weights on chain successfully!")
        else:
            bt.logging.error(f"set_weights failed {message}")

    def resync_hotkeys(self):
        bt.logging.info(
            "Metagraph updated, re-syncing hotkeys, dendrite pool and moving averages"
        )
        # Zero out all hotkeys that have been replaced.
        for uid, hotkey in enumerate(self.hotkeys):
            if self.miner_wps.get(uid) == None:
                self.miner_wps[uid] = []
            if hotkey != self.metagraph.hotkeys[uid]:
                self.miner_wps[uid] = []

        # Update the hotkeys.
        self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)

    def get_miner_uids(self) -> List[int]:
        available_uids = []
        assert self.config.neuron

        for uid in range(int(self.metagraph.n.item())):
            if uid == self.uid:
                continue

            # Filter non serving axons.
            if not self.metagraph.axons[uid].is_serving:
                continue
            # Filter validator permit > 1024 stake.
            if self.metagraph.validator_permit[uid]:
                if self.metagraph.S[uid] > self.config.neuron.vpermit_tao_limit:
                    continue
            available_uids.append(uid)
            continue
        return available_uids


if __name__ == "__main__":
    try:
        validator = Validator()
        validator.run()
    except Exception as e:
        bt.logging.error(str(e))
        bt.logging.error(traceback.format_exc())
    exit()
