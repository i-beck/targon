import time
import torch
import pprint
import random
import asyncio
import bittensor as bt
from typing import List
from targon.validator.config import env_config
from targon.validator import check_uid_availability
from targon.validator.crawler import VectorController
from targon.protocol import  TargonLinkPrediction, TargonSearchResult, TargonSearchResultStream

def get_random_uids(self, k: int, exclude: List[int] = None) -> torch.LongTensor:
    """Returns k available random uids from the metagraph.
    Args:
        k (int): Number of uids to return.
        exclude (List[int]): List of uids to exclude from the random sampling.
    Returns:
        uids (torch.LongTensor): Randomly sampled available uids.
    Notes:
        If `k` is larger than the number of available `uids`, set `k` to the number of available `uids`.
    """
    candidate_uids = []
    avail_uids = []

    for uid in range(self.metagraph.n.item()):
        uid_is_available = check_uid_availability(self.metagraph, uid, self.config.neuron.vpermit_tao_limit)
        uid_is_not_excluded = exclude is None or uid not in exclude

        if self.metagraph.axons[uid].coldkey in self.blacklisted_coldkeys:
            uid_is_available = False
            bt.logging.trace('blacklisted uid! not available', uid)

        if uid_is_available:
            avail_uids.append(uid)
            if uid_is_not_excluded:
                candidate_uids.append(uid)
                
    # Check if candidate_uids contain enough for querying, if not grab all avaliable uids
    available_uids = candidate_uids
    if len(candidate_uids) < k:
        available_uids += random.sample([uid for uid in avail_uids if uid not in candidate_uids], k-len(candidate_uids))

    uids = torch.tensor(random.sample(available_uids, k), dtype=torch.int64)
    return uids


async def search(self, synapse, axons):
    tasks = [asyncio.create_task(self.dendrite(axons=[axon], synapse=synapse, timeout=60, streaming=True)) for axon in axons]    
    return await asyncio.gather(*tasks)

async def _search_result_forward(self, question: str, sources: List[dict], uids: List[int]):
    search_synapse = TargonSearchResultStream(query=question, sources=sources, stream=True)
    axons = [self.metagraph.axons[uid] for uid in uids]
    search_results = await search(self, search_synapse, axons)

    full_responses = [await postprocess_result(self, result) for result in search_results]
    return full_responses

async def postprocess_result(self, responses):
    full_response = ""
    async for resp in responses:
        # Check if the response is a string
        if isinstance(resp, str):
            bt.logging.trace(resp)  # Logging the response
            full_response += resp  # Concatenating the response chunk
        else:
            full_response += "error"
    return full_response

def select_qa(self):
    '''Returns a question from the different tasks
    
    '''

    # randomly select which dataset to use self.coding_dataset, self.qa_dataset, self.reasoning_dataset
    dataset = random.choice([self.coding_dataset, self.qa_dataset, self.reasoning_dataset])
    data = next(dataset)
    return data

def generate_system_response(self, question):
    '''Generates a system response based on the task and solution
        # Conducting Single-Turn Conversation
            conversation = [ {'role': 'user', 'content': 'Hello?'} ] 

            prompt = tokenizer.apply_chat_template(conversation, tokenize=False, add_generation_prompt=True)

            inputs = tokenizer(prompt, return_tensors="pt").to(model.device) 
            outputs = model.generate(**inputs, use_cache=True, max_length=4096)
            output_text = tokenizer.decode(outputs[0]) 
            print(output_text)
    '''
    conversation = [ {'role': 'user', 'content': question} ]
    prompt = self.tokenizer.apply_chat_template(conversation, tokenize=False, add_generation_prompt=True)
    inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
    outputs = self.model.generate(**inputs, use_cache=True, max_length=4096)
    output_text = self.tokenizer.decode(outputs[0])
    return output_text



async def forward_fn(self, validation=True, stream=False):
    """Queries a list of uids for a question.
    Args:
        question (str): Question to query.
        uids (torch.LongTensor): Uids to query.
        timeout (float): Timeout for the query.
    Returns:
        responses (List[TargonQA]): List of responses.
    """
    k = 20 # TODO: change before release. for testing purposes only
    if validation:
        uids = get_random_uids(self, k=k).to(self.device)

        # validate Search Result responses
        data = select_qa(self)

        question = data['question']
        task = data['task']
        solution = data['solution']
        
        system_response = generate_system_response(self, question)
        bt.logging.trace('question', question)
        bt.logging.trace('task', task)
        bt.logging.trace('solution', solution)


        # Search Result
        # TODO: add support for sources
        sources = []
        completions = await _search_result_forward(self, question, sources, uids)
        bt.logging.info("completions", completions)

        # Compute the rewards for the responses given the prompt.
        rewards: torch.FloatTensor = torch.zeros(len(completions), dtype=torch.float32).to(self.device)
        for weight_i, reward_fn_i in zip(self.reward_weights, self.reward_functions):
            reward_i, reward_i_normalized = reward_fn_i.apply(system_response, completions)
            rewards += weight_i * reward_i_normalized.to(self.device)
            bt.logging.trace(str(reward_fn_i.name), reward_i.tolist())
            bt.logging.trace(str(reward_fn_i.name), reward_i_normalized.tolist())

        for masking_fn_i in self.masking_functions:
            mask_i, mask_i_normalized = masking_fn_i.apply(question, completions)
            rewards *= mask_i_normalized.to(self.device)  # includes diversity
            bt.logging.trace(str(masking_fn_i.name), mask_i_normalized.tolist())


        scattered_rewards: torch.FloatTensor = self.moving_averaged_scores.scatter(0, uids, rewards).to(self.device)

        # Update moving_averaged_scores with rewards produced by this step.
        # shape: [ metagraph.n ]
        alpha: float = self.config.neuron.moving_average_alpha
        self.moving_averaged_scores: torch.FloatTensor = alpha * scattered_rewards + (1 - alpha) * self.moving_averaged_scores.to(
            self.device
        )

        bt.logging.info("rewards", rewards.tolist())
        for i in range(10):
            bt.logging.info("sleeping for", i)
            time.sleep(1)
        





            






