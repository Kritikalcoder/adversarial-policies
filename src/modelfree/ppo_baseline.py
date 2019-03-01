"""Uses PPO to train an attack policy against a fixed victim policy."""

import datetime
import json
import os
import os.path as osp

from sacred import Experiment
from sacred.observers import FileStorageObserver
from stable_baselines import PPO2, logger
from stable_baselines.common.vec_env.vec_normalize import VecEnvWrapper

from aprl.envs.multi_agent import CurryVecEnv, FlattenSingletonVecEnv, make_subproc_vec_multi_env
from modelfree.gym_compete_conversion import GameOutcomeMonitor, GymCompeteToOurs
from modelfree.policy_loader import load_policy
from modelfree.scheduling import ConstantAnnealer, Scheduler
from modelfree.shaping_wrappers import apply_env_wrapper, apply_victim_wrapper
from modelfree.utils import make_env

ppo_baseline_ex = Experiment("ppo_baseline")
ppo_baseline_ex.observers.append(FileStorageObserver.create("data/sacred"))


class EmbedVictimWrapper(VecEnvWrapper):
    def __init__(self, multi_env, victim, victim_index):
        self.victim = victim
        curried_env = CurryVecEnv(multi_env, self.victim, agent_idx=victim_index)
        single_env = FlattenSingletonVecEnv(curried_env)

        super().__init__(single_env)

    def reset(self):
        return self.venv.reset()

    def step_wait(self):
        return self.venv.step_wait()

    def close(self):
        self.victim.sess.close()
        super().close()


@ppo_baseline_ex.capture
def train(_seed, env, out_dir, total_timesteps, num_env, policy,
          batch_size, load_path, learning_rate, rl_args, callbacks=None):
    kwargs = dict(env=env,
                  n_steps=batch_size // num_env,
                  verbose=1,
                  learning_rate=learning_rate,
                  **rl_args)
    if load_path is not None:
        # SOMEDAY: Counterintuitively this will inherit any extra arguments saved in the policy
        model = PPO2.load(load_path, **kwargs)
    else:
        model = PPO2(policy=policy, **kwargs)

    def callback(locals, globals):
        update = locals['update']
        checkpoint_dir = osp.join(out_dir, 'checkpoint')
        os.makedirs(checkpoint_dir, exist_ok=True)
        checkpoint_path = osp.join(checkpoint_dir, f'{update:05}')
        model.save(checkpoint_path)

        if callbacks is not None:
            for f in callbacks:
                f(locals, globals)

    model.learn(total_timesteps=total_timesteps, log_interval=1,
                seed=_seed, callback=callback)

    model_path = osp.join(out_dir, 'final_model.pkl')
    model.save(model_path)
    model.sess.close()
    return model_path


ISO_TIMESTAMP = "%Y%m%d_%H%M%S"


def setup_logger(out_dir="results", exp_name="test"):
    timestamp = datetime.datetime.now().strftime(ISO_TIMESTAMP)
    exp_name = exp_name.replace('/', '_')  # environment names can contain /'s
    out_dir = osp.join(out_dir, '{}-{}'.format(timestamp, exp_name))
    os.makedirs(out_dir, exist_ok=True)
    logger.configure(folder=osp.join(out_dir, 'mon'),
                     format_strs=['tensorboard', 'stdout'])
    return out_dir


@ppo_baseline_ex.named_config
def human_default():
    env = "multicomp/SumoHumans-v0"
    total_timesteps = int(1e8)
    num_env = 32
    batch_size = 8192
    _ = locals()
    del _


@ppo_baseline_ex.config
def default_ppo_config():
    env_name = "multicomp/SumoAnts-v0"   # Gym environment ID
    victim_type = "zoo"             # type supported by policy_loader.py
    victim_path = "1"               # path or other unique identifier
    victim_index = 0                # which agent the victim is (we default to other agent)
    num_env = 8                     # number of environments to run in parallel
    root_dir = "data/baselines"     # root of directory to store baselines log
    exp_name = "Dummy Exp Name"     # name of experiment
    total_timesteps = 4096          # total number of timesteps to train for
    policy = "MlpPolicy"            # policy network type
    batch_size = 2048               # batch size
    learning_rate = 3e-4            # learning rate
    rl_args = {}                    # extra RL algorithm arguments
    load_path = None  # path to load initial policy from
    seed = 0
    _ = locals()  # quieten flake8 unused variable warning
    del _


DEFAULT_CONFIGS = {
    'multicomp/KickAndDefend-v0': 'default_KickAndDefend.json',
    'multicomp/SumoHumans-v0': 'default_SumoHumans.json',
    'multicomp/SumoHumansAutoContact-v0': 'default_SumoHumans.json'
}


def load_default(env_name, config_dir):
    path_stem = os.path.join('experiments', config_dir)
    if env_name in DEFAULT_CONFIGS:
        default_config = DEFAULT_CONFIGS[env_name]
        fname = os.path.join(path_stem, default_config)
        with open(fname) as f:
            return json.load(f)
    else:
        return {}


@ppo_baseline_ex.config
def rew_shaping(env_name):
    rew_shape = False  # enable reward shaping
    victim_noise = False  # enable adding noise to victim
    rew_shape_params = load_default(env_name, 'rew_configs')  # parameters for reward shaping
    victim_noise_params = load_default(env_name, 'noise_configs')  # parameters for victim noise
    _ = locals()  # quieten flake8 unused variable warning
    del _


@ppo_baseline_ex.automain
def ppo_baseline(_run, env_name, victim_path, victim_type, victim_index, root_dir, exp_name,
                 learning_rate, num_env, seed, rew_shape, rew_shape_params,
                 victim_noise, victim_noise_params):
    out_dir = setup_logger(root_dir, exp_name)
    scheduler = Scheduler(func_dict={'lr': ConstantAnnealer(learning_rate).get_value})
    callbacks = []

    def env_fn(i):
        return make_env(env_name, seed, i, root_dir, pre_wrapper=GymCompeteToOurs)

    multi_env = make_subproc_vec_multi_env([lambda: env_fn(i) for i in range(num_env)])
    multi_env = GameOutcomeMonitor(multi_env, logger)
    callbacks.append(lambda locals, globals: multi_env.log_callback())

    # Get the correct victim and then wrap it accordingly.
    victim = load_policy(policy_path=victim_path, policy_type=victim_type, env=multi_env,
                         env_name=env_name, index=victim_index)
    if victim_noise:
        victim = apply_victim_wrapper(victim=victim, noise_params=victim_noise_params,
                                      scheduler=scheduler)

    # Get the correct environment and then wrap it accordingly.
    single_env = EmbedVictimWrapper(multi_env=multi_env, victim=victim,
                                    victim_index=victim_index)
    if rew_shape:
        agent_idx = 1 - victim_index
        single_env = apply_env_wrapper(single_env=single_env, shaping_params=rew_shape_params,
                                       agent_idx=agent_idx, logger=logger, scheduler=scheduler)
        callbacks.append(lambda locals, globals: single_env.log_callback())

    res = train(env=single_env, out_dir=out_dir, learning_rate=scheduler.get_func('lr'),
                callbacks=callbacks)
    single_env.close()

    return res
