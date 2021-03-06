# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os
import sys
import time
from pathlib import Path
from dataclasses import asdict
from typing import Iterator, Tuple, Callable, List, Optional
import math
import torch
from torch import nn
import wandb

import tube

from .params import (
    GameParams,
    ModelParams,
    OptimParams,
    SimulationParams,
    ExecutionParams,
)
from . import utils
from .env_creation_helpers import create_model, create_game, create_player
#######################################################################################
# OPTIMIZER CREATION
#######################################################################################


def create_optimizer(
    model: torch.jit.ScriptModule,
    optim_params: OptimParams,
    optim_state_dict: Optional[dict] = None,
) -> torch.optim.Optimizer:
    optim = torch.optim.Adam(
        model.parameters(), lr=optim_params.lr, eps=optim_params.eps
    )
    if optim_state_dict is not None:
        optim.load_state_dict(optim_state_dict)
    return optim


#######################################################################################
# TRAINING ENVIRONMENT CREATION
#######################################################################################


def create_training_environment(
    seed_generator: Iterator[int],
    model_path: Path,
    game_generation_devices: List[str],
    game_params: GameParams,
    simulation_params: SimulationParams,
    execution_params: ExecutionParams
) -> Tuple[tube.Context, tube.ChannelAssembler, Callable[[], List[int]]]:
    games = []
    context = tube.Context()
    print("Game generation devices: {}".format(game_generation_devices))
    server_listen_endpoint = execution_params.server_listen_endpoint
    server_connect_hostname = execution_params.server_connect_hostname
    opponent_model_path = execution_params.opponent_model_path
    is_server = server_listen_endpoint != ""
    is_client = server_connect_hostname != ""
    print("is_server is ", is_server)
    print("is_client is ", is_client)
    assembler = tube.ChannelAssembler(
        simulation_params.act_batchsize,
        len(game_generation_devices) if not is_server else 0,
        game_generation_devices,
        simulation_params.replay_capacity,
        next(seed_generator),
        str(model_path),
        simulation_params.train_channel_timeout_ms,
        simulation_params.train_channel_num_slots,
    )
    if is_server:
      assembler.start_server(server_listen_endpoint)
    if is_client:
      assembler.start_client(server_connect_hostname)
    if is_client and is_server:
      raise RuntimeError("Client and server parameters have both been specified")
    if not is_server:
      if opponent_model_path:
        print("loading opponent model")
        checkpoint = utils.load_checkpoint(checkpoint_path=opponent_model_path)
        model = create_model(
            game_params=checkpoint["game_params"],
            model_params=checkpoint["model_params"],
            resume_training=True,
            model_state_dict=checkpoint["model_state_dict"],
        )
        opponent_model_path = execution_params.checkpoint_dir / "model_opponent.pt"
        model.save(str(opponent_model_path))
      assembler_opponent = tube.ChannelAssembler(
          simulation_params.act_batchsize,
          len(game_generation_devices) if not is_server else 0,
          game_generation_devices,
          simulation_params.replay_capacity,
          next(seed_generator),
          str(opponent_model_path) if opponent_model_path else str(model_path),
          simulation_params.train_channel_timeout_ms,
          simulation_params.train_channel_num_slots,
      )
      assembler_opponent.set_is_tournament_opponent(True)
      if opponent_model_path:
        assembler_opponent.set_dont_request_model_updates(True)
      if is_client:
        assembler_opponent.start_client(server_connect_hostname)
    if not is_server:
      train_channel = assembler.get_train_channel()
      actor_channels = assembler.get_act_channels()
      actor_channel = actor_channels[0]

      for i in range(simulation_params.num_game):
          game = create_game(
              game_params,
              num_episode=-1,
              seed=next(seed_generator),
              eval_mode=False,
              per_thread_batchsize=simulation_params.per_thread_batchsize,
          )
          if simulation_params.per_thread_batchsize > 0:
              player_1 = create_player(
                  seed_generator=seed_generator,
                  game=game,
                  num_actor=simulation_params.num_actor,
                  num_rollouts=simulation_params.num_rollouts,
                  pure_mcts=False,
                  actor_channel=actor_channel,
                  assembler=assembler,
                  human_mode=False,
              )
              player_1.set_name("dev")
              if game.is_one_player_game():
                game.add_player(player_1, train_channel)
              else:
                player_2 = create_player(
                    seed_generator=seed_generator,
                    game=game,
                    num_actor=simulation_params.num_actor,
                    num_rollouts=simulation_params.num_rollouts,
                    pure_mcts=False,
                    actor_channel=actor_channel,
                    assembler=assembler_opponent,
                    human_mode=False,
                )
                player_2.set_name("opponent")
                if i % 2 == 0:
                  game.add_player(player_1, train_channel)
                  game.add_player(player_2, train_channel)
                else:
                  game.add_player(player_2, train_channel)
                  game.add_player(player_1, train_channel)
          else:
              player_1 = create_player(
                  seed_generator=seed_generator,
                  game=game,
                  num_actor=simulation_params.num_actor,
                  num_rollouts=simulation_params.num_rollouts,
                  pure_mcts=False,
                  actor_channel=actor_channels[i % len(actor_channels)],
                  assembler=None,
                  human_mode=False,
              )
              game.add_player(player_1, train_channel)
              if not game.is_one_player_game():
                  player_2 = create_player(
                      seed_generator=seed_generator,
                      game=game,
                      num_actor=simulation_params.num_actor,
                      num_rollouts=simulation_params.num_rollouts,
                      pure_mcts=False,
                      actor_channel=actor_channels[i % len(actor_channels)],
                      assembler=None,
                      human_mode=False,
                  )
                  game.add_player(player_2, train_channel)

          context.push_env_thread(game)
          games.append(game)

    def get_train_reward() -> Callable[[], List[int]]:
        nonlocal games
        reward = []
        for game in games:
            reward.append(game.get_result()[0])

        return reward

    return context, assembler, get_train_reward


#######################################################################################
# REPLAY BUFFER WARMING-UP
#######################################################################################


def warm_up_replay_buffer(
    assembler: tube.ChannelAssembler, replay_warmup: int, replay_buffer: Optional[bytes]
) -> None:
    if replay_buffer is not None:
        print("loading replay buffer...")
        assembler.buffer = replay_buffer
    assembler.start()
    prev_buffer_size = -1
    t = t_init = time.time()
    t0 = -1
    size0 = 0
    while assembler.buffer_size() < replay_warmup:
        buffer_size = assembler.buffer_size()
        if buffer_size != prev_buffer_size:  # avoid flooding stdout
            if buffer_size > 10000 and t0 == -1:
                size0 = buffer_size
                t0 = time.time()
            prev_buffer_size = max(prev_buffer_size, 0)
            frame_rate = (buffer_size - prev_buffer_size) / (time.time() - t)
            frame_rate = int(frame_rate)
            prev_buffer_size = buffer_size
            t = time.time()
            duration = t - t_init
            print(
                f"warming-up replay buffer: {(buffer_size * 100) // replay_warmup}% "
                f"({buffer_size}/{replay_warmup}) in {duration:.2f}s "
                f"- speed: {frame_rate} frames/s",
                end="\r",
                flush=True,
            )
        time.sleep(2)
    print(
        f"replay buffer warmed up: 100% "
        f"({assembler.buffer_size()}/{replay_warmup})"
        "                                                                          "
    )
    print(
        "avg speed: %.2f frames/s"
        % ((assembler.buffer_size() - size0) / (time.time() - t0))
    )


#######################################################################################
# TRAINING
#######################################################################################

class ModelWrapperForDDP(nn.Module):
    def __init__(self, module):
        super().__init__()
        self.module = module
    def forward(self, x: torch.Tensor):
        return self.module._forward(x, True)

class DDPWrapperForModel(nn.Module):
    def __init__(self, module):
        super().__init__()
        self.module = module
    def _forward(self, x: torch.Tensor, return_logit: bool):
        if not return_logit:
            raise RuntimeError("DDPWrapperForModel: return_logit is false")
        return self.module.forward(x)

class PerfectPlayerWrapper:
  def __init__(self):
    sys.path.append("/home/gemas/connect4")
    from PerfectPlayer import PerfectPlayer
    self.perfect_player = PerfectPlayer("/home/gemas/connect4/7x6.book")

  def _value_error(self, v, p_v):
    return nn.functional.mse_loss(p_v, v, reduction="none").squeeze(1)


  def _logit_policy_error(self, pred_logit, p_pi):
    pred_log_pi = nn.functional.log_softmax(pred_logit.flatten(1), dim=1).view_as(pred_logit)
    return torch.nn.KLDivLoss(reduction="batchmean")(pred_log_pi,p_pi)
  
  def _policy_error(self, pred_logit, p_pi):
    error =  torch.nn.KLDivLoss(reduction="batchmean")((pred_logit + 0.0000001).log(),p_pi)
    if math.isinf(error.detach().mean().item()):
      breakpoint()

    return error

  def loss(
    self, 
    move_history: torch.Tensor,
    pred_pi: torch.Tensor,
    pred_v: torch.Tensor,
    mcts_pi: torch.Tensor,
    mcts_v: torch.Tensor,
    stat: utils.MultiCounter
    ) -> None:
    _, perfect_v, perfect_pi = self.perfect_player.get_position_scores(move_history.cpu().numpy())
    perfect_v, perfect_pi = torch.from_numpy(perfect_v).float(), torch.from_numpy(perfect_pi).float() 
    #Perfect Player vs NN
    v_err = self._value_error(pred_v.cpu(), perfect_v)
    pi_err = self._policy_error(pred_pi.cpu(), perfect_pi) 
    stat["nn_pp_v_err"].feed(v_err.detach().mean().item())
    stat["nn_pp_pi_err"].feed(pi_err.detach().mean().item())

    #Perfect Player vs MCTS
    v_err = self._value_error(mcts_v.cpu(), perfect_v)
    pi_err = self._policy_error(mcts_pi.view_as(perfect_pi).cpu(), perfect_pi) 
    stat["mcts_pp_v_err"].feed(v_err.detach().mean().item())
    stat["mcts_pp_pi_err"].feed(pi_err.detach().mean().item())


_train_epoch_waiting_time = 0
#_perfect_player = PerfectPlayerWrapper() 
def _train_epoch(
    train_device: torch.device,
    model: torch.jit.ScriptModule,
    ddpmodel: ModelWrapperForDDP,
    model_path: Path,
    optim: torch.optim.Optimizer,
    assembler: tube.ChannelAssembler,
    stat: utils.MultiCounter,
    epoch: int,
    optim_params: OptimParams,
    sync_period: int,
) -> None:
    global _train_epoch_waiting_time
    #global _perfect_player
    pre_num_add = assembler.buffer_num_add()
    pre_num_sample = assembler.buffer_num_sample()
    sync_s = 0.
    num_sync = 0
    t = time.time()
    time.sleep(_train_epoch_waiting_time)
    lossmodel = DDPWrapperForModel(ddpmodel) if ddpmodel is not None else model
    for eid in range(optim_params.epoch_len):
        batch = assembler.sample(optim_params.batchsize)
        batch = utils.to_device(batch, train_device)
        loss, pred_pi, pred_v = model.loss(lossmodel, batch["s"], batch["v"], batch["pi"], batch["pi_mask"], stat)
       # _perfect_player.loss(batch['m_h'], pred_pi, pred_v, batch["pi"], batch["v"], stat)
        loss.backward()
        grad_norm = nn.utils.clip_grad_norm_(model.parameters(), optim_params.grad_clip)
        optim.step()
        optim.zero_grad()

        if (epoch * optim_params.epoch_len + eid + 1) % sync_period == 0:
            sync_t0 = time.time()
            assembler.update_model(model.state_dict())
            sync_s += time.time() - sync_t0
            num_sync += 1


        stat["loss"].feed(loss.detach().item())
        stat["grad_norm"].feed(grad_norm)

    post_num_add = assembler.buffer_num_add()
    post_num_sample = assembler.buffer_num_sample()
    time_elapsed = time.time() - t
    delta_add = post_num_add - pre_num_add
    print("buffer add rate: %.2f / s" % (delta_add / time_elapsed))
    delta_sample = post_num_sample - pre_num_sample
    if delta_sample > 8 * delta_add:   # If the sample rate is not at least 8x the add rate, everything is fine.
        _train_epoch_waiting_time += time_elapsed
    else:
        _train_epoch_waiting_time = 0
    print("buffer sample rate: %.2f / s" % (delta_sample / time_elapsed))
    print(f"syncing duration: {sync_s:2f}s for {num_sync} syncs ({int(100 * sync_s / time_elapsed)}% of train time)")

    stat.summary(epoch)
    wandb.log({"epoch": epoch, "loss": stat["loss"].mean(), "grad_norm": stat["grad_norm"].mean()})
    stat.reset()

def train_model(
    command_history: utils.CommandHistory,
    start_time: float,
    train_device: torch.device,
    model: torch.jit.ScriptModule,
    model_path: Path,
    ddpmodel,
    optim: torch.optim.Optimizer,
    context: tube.Context,
    assembler: tube.ChannelAssembler,
    get_train_reward: Callable[[], List[int]],
    game_params: GameParams,
    model_params: ModelParams,
    optim_params: OptimParams,
    simulation_params: SimulationParams,
    execution_params: ExecutionParams,
    epoch: int = 0,
) -> None:
    stat = utils.MultiCounter(execution_params.checkpoint_dir)
    max_time = execution_params.max_time
    init_epoch = epoch
    while max_time is None or time.time() < start_time + max_time:
        if epoch - init_epoch >= optim_params.num_epoch:
            break
        epoch += 1
        if not (epoch - init_epoch) % execution_params.saving_period:
            assembler.add_tournament_model("e%d" % (epoch), model.state_dict())
            utils.save_checkpoint(
                command_history=command_history,
                epoch=epoch,
                model=model,
                optim=optim,
                assembler=assembler,
                game_params=game_params,
                model_params=model_params,
                optim_params=optim_params,
                simulation_params=simulation_params,
                execution_params=execution_params,
            )
        _train_epoch(
            train_device=train_device,
            model=model,
            ddpmodel=ddpmodel,
            model_path=model_path,
            optim=optim,
            assembler=assembler,
            stat=stat,
            epoch=epoch,
            optim_params=optim_params,
            sync_period=simulation_params.sync_period,
        )
        # resource usage stats
        print("Resource usage:")
        print(utils.get_res_usage_str())
        print("Context stats:")
        print(context.get_stats_str())
        # train result
        print(
            ">>>train: epoch: %d, %s" % (epoch, utils.Result(get_train_reward()).log()),
            flush=True,
        )
    # checkpoint last state
    utils.save_checkpoint(
        command_history=command_history,
        epoch=epoch,
        model=model,
        optim=optim,
        assembler=assembler,
        game_params=game_params,
        model_params=model_params,
        optim_params=optim_params,
        simulation_params=simulation_params,
        execution_params=execution_params,
    )


#######################################################################################
# OVERALL TRAINING WORKFLOW
#######################################################################################


def run_training(
    command_history: utils.CommandHistory,
    game_params: GameParams,
    model_params: ModelParams,
    optim_params: OptimParams,
    simulation_params: SimulationParams,
    execution_params: ExecutionParams,
    run_group = "Default Group"
) -> None:
    wandb.init(project="thesis-az", group=run_group)
    cfg = {
      **asdict(game_params),
      **asdict(model_params),
      **asdict(optim_params),
      **asdict(simulation_params),
      **asdict(execution_params),
    }

    wandb.config.update(cfg) 
    start_time = time.time()
    logger_path = os.path.join(execution_params.checkpoint_dir, "train.log")
    sys.stdout = utils.Logger(logger_path)

    print("#" * 70)
    print("#" + "TRAINING".center(68) + "#")
    print("#" * 70)

    print("setting-up pseudo-random generator...")
    seed_generator = utils.generate_random_seeds(seed=execution_params.seed)

    # checkpoint, resume from where it stops
    epoch = 0
    ckpts = list(utils.gen_checkpoints(checkpoint_dir=execution_params.checkpoint_dir, only_last=True, real_time=False))
    checkpoint = {}
    if ckpts:
        checkpoint = ckpts[0]
        former_command_history = checkpoint["command_history"]
        command_history.build_history(former_command_history)
        optim_params = command_history.update_params_from_checkpoint(
            checkpoint_params=checkpoint["optim_params"], resume_params=optim_params
        )
        simulation_params = command_history.update_params_from_checkpoint(
            checkpoint_params=checkpoint["simulation_params"],
            resume_params=simulation_params,
        )
        execution_params = command_history.update_params_from_checkpoint(
            checkpoint_params=checkpoint["execution_params"],
            resume_params=execution_params,
        )
    if command_history.last_command_contains("init_checkpoint"):
        if ckpts:
            raise RuntimeError("Cannot restart from init_checkpoint, already restarting from non-empty checkpoint_dir")
        # pretrained model, consider new training from epoch zero
        print("loading pretrained model from checkpoint...")
        checkpoint = utils.load_checkpoint(checkpoint_path=model_params.init_checkpoint)
    if checkpoint:
        # game_params and model_params cannot change on a checkpoint
        # either write the same, or don't specify them
        ignored = {"init_checkpoint", "game_name"}  # this one can change
        current_params = dict(game_params=game_params, model_params=model_params)
        for params_name, params in current_params.items():
            for attr, val in asdict(params).items():
                if command_history.last_command_contains(attr) and attr not in ignored:
                    ckpt_val = getattr(checkpoint[params_name], attr)
                    assert val == ckpt_val, f"When resuming, got '{val}' for {attr} but cannot override from past run with '{ckpt_val}'."
        specified_game_name = game_params.game_name
        game_params = checkpoint["game_params"]
        if specified_game_name is not None:
          game_params.game_name = specified_game_name
        model_params = checkpoint["model_params"]
        epoch = checkpoint["epoch"]
        print("reconstructing the model...")
    else:
        print("creating and saving the model...")
    train_device = execution_params.device[0]
    game_generation_devices = (
        [train_device]
        if len(execution_params.device) == 1
        else execution_params.device[1:]
    )
    train_device = torch.device(train_device)
    model = create_model(
        game_params=game_params,
        model_params=model_params,
        resume_training=bool(checkpoint),
        model_state_dict=checkpoint["model_state_dict"] if checkpoint else None,
    ).to(train_device)
    model_path = execution_params.checkpoint_dir / "model.pt"
    model.save(str(model_path))

    ddpmodel = None

    if execution_params.ddp:
        torch.distributed.init_process_group(backend="nccl")
        ddpmodel = nn.parallel.DistributedDataParallel(ModelWrapperForDDP(model))

    print("creating optimizer...")
    optim = create_optimizer(
        model=model,
        optim_params=optim_params,
        optim_state_dict=checkpoint.get("optim_state_dict", None),
    )

    print("creating training environment...")
    context, assembler, get_train_reward = create_training_environment(
        seed_generator=seed_generator,
        model_path=model_path,
        game_generation_devices=game_generation_devices,
        game_params=game_params,
        simulation_params=simulation_params,
        execution_params=execution_params
    )
    assembler.update_model(model.state_dict())
    assembler.add_tournament_model("init", model.state_dict())
    context.start()

    print("warming-up replay buffer...")
    warm_up_replay_buffer(
        assembler=assembler,
        replay_warmup=simulation_params.replay_warmup,
        replay_buffer=checkpoint.get("replay_buffer", None),
    )

    print("training model...")
    train_model(
        command_history=command_history,
        start_time=start_time,
        train_device=train_device,
        model=model,
        ddpmodel=ddpmodel,
        model_path=model_path,
        optim=optim,
        context=context,
        assembler=assembler,
        get_train_reward=get_train_reward,
        game_params=game_params,
        model_params=model_params,
        optim_params=optim_params,
        simulation_params=simulation_params,
        execution_params=execution_params,
        epoch=epoch
    )

    elapsed_time = time.time() - start_time
    print(f"total time: {elapsed_time} s")
