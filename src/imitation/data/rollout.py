"""Methods to collect, analyze and manipulate transition and trajectory rollouts."""

import collections
import dataclasses
import logging
from typing import Callable, Dict, Hashable, List, Mapping, Optional, Sequence, Union

import numpy as np
from stable_baselines3.common.base_class import BaseAlgorithm
from stable_baselines3.common.policies import BasePolicy
from stable_baselines3.common.utils import check_for_correct_spaces
from stable_baselines3.common.vec_env import VecEnv
from imitation.data import types

from compression.policies.ExpertPolicy import ExpertPolicy
from compression.policies.StudentPolicy import StudentPolicy

def unwrap_traj(traj: types.TrajectoryWithRew) -> types.TrajectoryWithRew:
    """Uses `RolloutInfoWrapper`-captured `obs` and `rews` to replace fields.

    This can be useful for bypassing other wrappers to retrieve the original
    `obs` and `rews`.

    Fails if `infos` is None or if the trajectory was generated from an
    environment without imitation.util.rollout.RolloutInfoWrapper

    Args:
        traj: A trajectory generated from `RolloutInfoWrapper`-wrapped Environments.

    Returns:
        A copy of `traj` with replaced `obs` and `rews` fields.
    """
    ep_info = traj.infos[-1]["rollout"]
    res = dataclasses.replace(traj, obs=ep_info["obs"], rews=ep_info["rews"])
    assert len(res.obs) == len(res.acts) + 1
    assert len(res.rews) == len(res.acts)
    return res


class TrajectoryAccumulator:
    """Accumulates trajectories step-by-step.

    Useful for collecting completed trajectories while ignoring partially-completed
    trajectories (e.g. when rolling out a VecEnv to collect a set number of
    transitions). Each in-progress trajectory is identified by a 'key', which enables
    several independent trajectories to be collected at once. They key can also be left
    at its default value of `None` if you only wish to collect one trajectory.
    """

    def __init__(self):
        """Initialise the trajectory accumulator."""
        self.partial_trajectories = collections.defaultdict(list)

    def add_step(
        self,
        step_dict: Mapping[str, np.ndarray],
        key: Hashable = None,
    ) -> None:
        """Add a single step to the partial trajectory identified by `key`.

        Generally a single step could correspond to, e.g., one environment managed
        by a VecEnv.

        Args:
            step_dict: dictionary containing information for the current step. Its
                keys could include any (or all) attributes of a `TrajectoryWithRew`
                (e.g. "obs", "acts", etc.).
            key: key to uniquely identify the trajectory to append to, if working
                with multiple partial trajectories.
        """
        self.partial_trajectories[key].append(step_dict)

    def finish_trajectory(
        self,
        key: Hashable,
        terminal: bool,
    ) -> types.TrajectoryWithRew:
        """Complete the trajectory labelled with `key`.

        Args:
            key: key uniquely identifying which in-progress trajectory to remove.
            terminal: trajectory has naturally finished (i.e. includes terminal state).

        Returns:
            traj: list of completed trajectories popped from
                `self.partial_trajectories`.
        """
        part_dicts = self.partial_trajectories[key]
        del self.partial_trajectories[key]
        out_dict_unstacked = collections.defaultdict(list)
        for part_dict in part_dicts:
            for key, array in part_dict.items():
                out_dict_unstacked[key].append(array)
        out_dict_stacked = {
            key: np.stack(arr_list, axis=0)
            for key, arr_list in out_dict_unstacked.items()
        }
        traj = types.TrajectoryWithRew(**out_dict_stacked, terminal=terminal)
        assert traj.rews.shape[0] == traj.acts.shape[0] == traj.obs.shape[0] - 1
        return traj

    def add_steps_and_auto_finish(
        self,
        acts: np.ndarray,
        obs: np.ndarray,
        rews: np.ndarray,
        dones: np.ndarray,
        infos: List[dict],
    ) -> List[types.TrajectoryWithRew]:
        """Calls `add_step` repeatedly using acts and the returns from `venv.step`.

        Also automatically calls `finish_trajectory()` for each `done == True`.
        Before calling this method, each environment index key needs to be
        initialized with the initial observation (usually from `venv.reset()`).

        See the body of `util.rollout.generate_trajectory` for an example.

        Args:
            acts: Actions passed into `VecEnv.step()`.
            obs: Return value from `VecEnv.step(acts)`.
            rews: Return value from `VecEnv.step(acts)`.
            dones: Return value from `VecEnv.step(acts)`.
            infos: Return value from `VecEnv.step(acts)`.

        Returns:
            A list of completed trajectories. There should be one trajectory for
            each `True` in the `dones` argument.
        """
        trajs = []
        for env_idx in range(len(obs)):
            assert env_idx in self.partial_trajectories
            assert list(self.partial_trajectories[env_idx][0].keys()) == ["obs"], (
                "Need to first initialize partial trajectory using "
                "self._traj_accum.add_step({'obs': ob}, key=env_idx)"
            )

        zip_iter = enumerate(zip(acts, obs, rews, dones, infos))
        for env_idx, (act, ob, rew, done, info) in zip_iter:
            if done:
                # When dones[i] from VecEnv.step() is True, obs[i] is the first
                # observation following reset() of the ith VecEnv, and
                # infos[i]["terminal_observation"] is the actual final observation.
                real_ob = info["terminal_observation"]
            else:
                real_ob = ob

            self.add_step(
                dict(
                    acts=act,
                    rews=rew,
                    # this is not the obs corresponding to `act`, but rather the obs
                    # *after* `act` (see above)
                    obs=real_ob,
                    infos=info,
                ),
                env_idx,
            )
            if done:
                # finish env_idx-th trajectory
                new_traj = self.finish_trajectory(env_idx, terminal=True)
                trajs.append(new_traj)
                # When done[i] from VecEnv.step() is True, obs[i] is the first
                # observation following reset() of the ith VecEnv.
                self.add_step(dict(obs=ob), env_idx)
        return trajs


GenTrajTerminationFn = Callable[[Sequence[types.TrajectoryWithRew]], bool]


def make_min_episodes(n: int) -> GenTrajTerminationFn:
    """Terminate after collecting n episodes of data.

    Args:
        n: Minimum number of episodes of data to collect.
            May overshoot if two episodes complete simultaneously (unlikely).

    Returns:
        A function implementing this termination condition.
    """
    assert n >= 1
    return lambda trajectories: len(trajectories) >= n


def make_min_timesteps(n: int) -> GenTrajTerminationFn:
    """Terminate at the first episode after collecting n timesteps of data.

    Args:
        n: Minimum number of timesteps of data to collect.
            May overshoot to nearest episode boundary.

    Returns:
        A function implementing this termination condition.
    """
    assert n >= 1

    def f(trajectories: Sequence[types.TrajectoryWithRew]):
        timesteps = sum(len(t.obs) - 1 for t in trajectories)
        return timesteps >= n

    return f


def make_sample_until(
    min_timesteps: Optional[int],
    min_episodes: Optional[int],
) -> GenTrajTerminationFn:
    """Returns a termination condition sampling for a number of timesteps and episodes.

    Args:
        min_timesteps: Sampling will not stop until there are at least this many
            timesteps.
        min_episodes: Sampling will not stop until there are at least this many
            episodes.

    Returns:
        A termination condition.

    Raises:
        ValueError: Neither of n_timesteps and n_episodes are set, or either are
            non-positive.
    """
    if min_timesteps is None and min_episodes is None:
        raise ValueError(
            "At least one of min_timesteps and min_episodes needs to be non-None",
        )

    conditions = []
    if min_timesteps is not None:
        if min_timesteps <= 0:
            raise ValueError(
                f"min_timesteps={min_timesteps} if provided must be positive",
            )
        conditions.append(make_min_timesteps(min_timesteps))

    if min_episodes is not None:
        if min_episodes <= 0:
            raise ValueError(
                f"min_episodes={min_episodes} if provided must be positive",
            )
        conditions.append(make_min_episodes(min_episodes))

    def sample_until(trajs: Sequence[types.TrajectoryWithRew]) -> bool:
        for cond in conditions:
            if not cond(trajs):
                return False
        return True

    return sample_until


# A PolicyCallable is a function that takes an array of observations
# and returns an array of corresponding actions.
PolicyCallable = Callable[[np.ndarray], np.ndarray]
AnyPolicy = Union[BaseAlgorithm, BasePolicy, PolicyCallable, None]

def _policy_to_callable(
    policy: AnyPolicy,
    venv: VecEnv,
    deterministic_policy: bool,
    computation_time_verbose: bool = False,
) -> PolicyCallable:
    """Converts any policy-like object into a function from observations to actions."""
    if policy is None:

        def get_actions(states):
            acts = [venv.action_space.sample() for _ in range(len(states))]
            return np.stack(acts, axis=0)

    # elif hasattr(policy, 'predictSeveral'): #to avoid importing ExpertPolicy and StudentPolicy in this file
    elif isinstance(policy, StudentPolicy):

        def get_actions(*args):
            states, num_obses = args
            acts, mean_computation_time = policy.predictSeveralWithComputationTimeVerbose(  # pytype: disable=attribute-error
                states,
                num_obses=num_obses,
                deterministic=deterministic_policy
            )

            if computation_time_verbose:
                return acts, mean_computation_time
            else:
                return acts
    
    elif isinstance(policy, ExpertPolicy):

        def get_actions(*args):
            states = args[0]
            acts, mean_computation_time = policy.predictSeveralWithComputationTimeVerbose(  # pytype: disable=attribute-error
                states,
                deterministic=deterministic_policy
            )

            if computation_time_verbose:
                return acts, mean_computation_time
            else:
                return acts
      

    elif isinstance(policy, (BaseAlgorithm, BasePolicy)):
        # There's an important subtlety here: BaseAlgorithm and BasePolicy
        # are themselves Callable (which we check next). But in their case,
        # we want to use the .predict() method, rather than __call__()
        # (which would call .forward()). So this elif clause must come first!

        def get_actions(states):
            # pytype doesn't seem to understand that policy is a BaseAlgorithm
            # or BasePolicy here, rather than a Callable
            acts, _ = policy.predict(  # pytype: disable=attribute-error
                states,
                deterministic=deterministic_policy,
            )
            return acts

    elif isinstance(policy, Callable):
        get_actions = policy

    else:
        raise TypeError(
            "Policy must be None, a stable-baselines policy or algorithm, "
            f"or a Callable, got {type(policy)} instead",
        )

    if isinstance(policy, BaseAlgorithm):
        # check that the observation and action spaces of policy and environment match
        check_for_correct_spaces(venv, policy.observation_space, policy.action_space)

    return get_actions


def generate_trajectories(
    policy: AnyPolicy,
    venv: VecEnv, #Note that a InteractiveTrajectoryCollector is also valid here
    sample_until: GenTrajTerminationFn = None, #not used anymore
    *,
    deterministic_policy: bool = False,
    rng: np.random.RandomState = np.random,
    total_demos_per_round = float("inf"),
) -> Sequence[types.TrajectoryWithRew]:
    """Generate trajectory dictionaries from a policy and an environment.

    Args:
        policy: Can be any of the following:
            1) A stable_baselines3 policy or algorithm trained on the gym environment.
            2) A Callable that takes an ndarray of observations and returns an ndarray
            of corresponding actions.
            3) None, in which case actions will be sampled randomly.
        venv: The vectorized environments to interact with.
        sample_until: A function determining the termination condition.
            It takes a sequence of trajectories, and returns a bool.
            Most users will want to use one of `min_episodes` or `min_timesteps`.
        deterministic_policy: If True, asks policy to deterministically return
            action. Note the trajectories might still be non-deterministic if the
            environment has non-determinism!
        rng: used for shuffling trajectories.

    Returns:
        Sequence of trajectories, satisfying `sample_until`. Additional trajectories
        may be collected to avoid biasing process towards short episodes; the user
        should truncate if required.
    """
    get_actions = _policy_to_callable(policy, venv, deterministic_policy)

    computation_time_verbose = False

    # Collect rollout tuples.
    trajectories = []
    # accumulator for incomplete trajectories
    trajectories_accum = TrajectoryAccumulator()
    obs = venv.reset()
    for env_idx, ob in enumerate(obs):
        # Seed with first obs only. Inside loop, we'll only add second obs from
        # each (s,a,r,s') tuple, under the same "obs" key again. That way we still
        # get all observations, but they're not duplicated into "next obs" and
        # "previous obs" (this matters for, e.g., Atari, where observations are
        # really big).
        trajectories_accum.add_step(dict(obs=ob), env_idx)

    # Now, we sample until `sample_until(trajectories)` is true.
    # If we just stopped then this would introduce a bias towards shorter episodes,
    # since longer episodes are more likely to still be active, i.e. in the process
    # of being sampled from. To avoid this, we continue sampling until all epsiodes
    # are complete.
    #
    # To start with, all environments are active.
    active = np.ones(venv.num_envs, dtype=bool)
    num_demos=0
    while np.any(active) and num_demos<total_demos_per_round:

        print(f"Number of demos: {num_demos}/{total_demos_per_round}")

        num_obses = venv.env_method("get_num_obs")
        num_max_of_obsts = venv.env_method("get_num_max_of_obst")
        CPs_per_obstacle = venv.env_method("get_CPs_per_obstacle") #this is a list, but all the elements are the same

        if computation_time_verbose:
            acts, computation_time = get_actions(obs, num_obses)
        else:
            acts = get_actions(obs, num_obses)

        for i in range(len(acts)):
            #acts[i,:,:] is the action of environment i
            if(np.isnan(np.sum(acts[i,:,:]))==False):
                num_demos+=1

        if(num_demos>=total_demos_per_round): #To avoid dropping partial trajectories
            venv.env_method("forceDone") 

        ##
        ## get obs, rewards, dones, infos
        ## this will call step() in InteractiveTrajectoryCollector (see dagger.py)
        ## and will eventually call step_wait() in InteractiveTrajectoryCollector (see dagger.py)
        ##

        obs, rews, dones, infos = venv.step(acts) 

        # If an environment is inactive, i.e. the episode completed for that
        # environment after `sample_until(trajectories)` was true, then we do
        # *not* want to add any subsequent trajectories from it. We avoid this
        # by just making it never done.
        # (jtorde) But note that that env will be reset and will keep being called
        # (jtorde) and more demos will keep being saved in the InteractiveTrajectoryCollector (venv variable in this function)-->step_wait function (see dagger.py)
        # Note that only the environments that have done==True are the ones that are finished in add_steps_and_auto_finish 
        dones &= active

        new_trajs = trajectories_accum.add_steps_and_auto_finish(
            acts,
            obs,
            rews,
            dones,
            infos,
        )
        trajectories.extend(new_trajs)

        if sample_until is not None:
            if sample_until(trajectories):
                # Termination condition has been reached. Mark as inactive any environments
                # where a trajectory was completed this timestep.
                active &= ~dones

    # jtorde
    # Note that all the demos are being saved in InteractiveTrajectoryCollector (venv variable in this function)-->step_wait
    # That means that, even if len(trajectories)==0 (because none of them finished), we have already saved all the valid demos 
    ######

    # Each trajectory is sampled i.i.d.; however, shorter episodes are added to
    # `trajectories` sooner. Shuffle to avoid bias in order. This is important
    # when callees end up truncating the number of trajectories or transitions.
    # It is also cheap, since we're just shuffling pointers.
    rng.shuffle(trajectories)

    # Sanity checks.
    # for trajectory in trajectories:
    #     n_steps = len(trajectory.acts)
    #     # extra 1 for the end
    #     exp_obs = (n_steps + 1,) + venv.observation_space.shape
    #     real_obs = trajectory.obs.shape
    #     assert real_obs == exp_obs, f"expected shape {exp_obs}, got {real_obs}"
    #     exp_act = (n_steps,) + venv.action_space.shape
    #     real_act = trajectory.acts.shape
    #     assert real_act == exp_act, f"expected shape {exp_act}, got {real_act}"
    #     exp_rew = (n_steps,)
    #     real_rew = trajectory.rews.shape
    #     assert real_rew == exp_rew, f"expected shape {exp_rew}, got {real_rew}"

    return trajectories

def generate_trajectories_for_benchmark(
    policy: AnyPolicy,
    venv: VecEnv, #Note that a InteractiveTrajectoryCollector is also valid here
    sample_until: GenTrajTerminationFn = None, #not used anymore
    *,
    deterministic_policy: bool = False,
    rng: np.random.RandomState = np.random,
    total_demos = float("inf"),
) -> Sequence[types.TrajectoryWithRew]:
    """
    Changed from generate_trajectories() to generate trajectories for benchmarking
    
    Generate trajectory dictionaries from a policy and an environment.

    Args:
        policy: Can be any of the following:
            1) A stable_baselines3 policy or algorithm trained on the gym environment.
            2) A Callable that takes an ndarray of observations and returns an ndarray
            of corresponding actions.
            3) None, in which case actions will be sampled randomly.
        venv: The vectorized environments to interact with.
        sample_until: A function determining the termination condition.
            It takes a sequence of trajectories, and returns a bool.
            Most users will want to use one of `min_episodes` or `min_timesteps`.
        deterministic_policy: If True, asks policy to deterministically return
            action. Note the trajectories might still be non-deterministic if the
            environment has non-determinism!
        rng: used for shuffling trajectories.

    Returns:
        Sequence of trajectories, satisfying `sample_until`. Additional trajectories
        may be collected to avoid biasing process towards short episodes; the user
        should truncate if required.
    """

    ##
    ## Convert policy to a callable.
    ##

    computation_time_verbose = True
    get_actions = _policy_to_callable(policy, venv, deterministic_policy, computation_time_verbose=computation_time_verbose)

    ##
    ## Initilaze trajectory list to collect rollout tuples.
    ## This will contrain all the finished trajectories (ref: add_steps_and_auto_finish())
    ##

    trajectories = []

    ##
    ## accumulator for incomplete trajectories
    ##

    trajectories_accum = TrajectoryAccumulator()
    f_obs = venv.reset()
    
    for env_idx, ob in enumerate(f_obs):
        # Seed with first obs only. Inside loop, we'll only add second obs from
        # each (s,a,r,s') tuple, under the same "obs" key again. That way we still
        # get all observations, but they're not duplicated into "next obs" and
        # "previous obs" (this matters for, e.g., Atari, where observations are
        # really big).
        trajectories_accum.add_step(dict(obs=ob), env_idx)

    # Now, we sample until `sample_until(trajectories)` is true.
    # If we just stopped then this would introduce a bias towards shorter episodes,
    # since longer episodes are more likely to still be active, i.e. in the process
    # of being sampled from. To avoid this, we continue sampling until all epsiodes
    # are complete.
    #
    # To start with, all environments are active.
    active = np.ones(venv.num_envs, dtype=bool)
    num_demos=0
    total_obs_avoidance_failure=0
    total_trans_dyn_limit_failure=0
    total_yaw_dyn_limit_failure=0
    total_failure=0
    computation_times = []
    costs = []

    while np.any(active) and num_demos < total_demos:

        ##
        ## To make sure benchmarking is fair, we reset the environemnt
        ##

        f_obs = venv.reset()

        ##
        ## in getFutureWPosStaticObstacles() and getFutureWPosDynamicObstacles(), we added dummy obstacles to meet the max number of obstacles
        ## because (1) Expert needs a fixed number of obstacles to generate actions, and (2) venv needs a fixed number of observation space
        ## For expert, this is not a problem, but for student, we need to get rid of redundant observations
		## And it is done in predictSeveral() and _predict() in StudentPolicy.py
        ##

        num_obses = venv.env_method("get_num_obs")
        num_max_of_obsts = venv.env_method("get_num_max_of_obst")
        CPs_per_obstacle = venv.env_method("get_CPs_per_obstacle") #this is a list, but all the elements are the same

        if computation_time_verbose:
            f_acts, computation_time = get_actions(f_obs, num_obses)
        else:
            f_acts = get_actions(f_obs, num_obses)

        is_nan_action = False
        for i in range(len(f_acts)): #loop over all the environments
            # f_acts[i,:,:] is the action of environment i
            if(np.isnan(np.sum(f_acts[i,:,:]))==False):
                num_demos+=1
            else:
                total_failure+=1
                is_nan_action = True

        print(f"Number of demos: {num_demos}/{total_demos}")

        if(num_demos >= total_demos): #To avoid dropping partial trajectories
            venv.env_method("forceDone") 

        ##
        ## get obs, rewards, dones, infos
        ## this will call step() in InteractiveTrajectoryCollector (see dagger.py)
        ## and will eventually call step_wait() in InteractiveTrajectoryCollector (see dagger.py)
        ##
        
        f_obs, rews, dones, infos = venv.step(f_acts) # in here it will choose the best action out of f_acts and calculate rewards for the best action

        for i in range(venv.num_envs):
            venv.env_method("saveInBag", f_acts[i], indices=[i]) 
            
        ##
        ## calculate the total number of obs_avoidance_failure and dyn_limit_failure
        ##

        for i in range(len(infos)):
            if infos[i]["obst_avoidance_violation"]:
                total_obs_avoidance_failure+=1
            if infos[i]["trans_dyn_lim_violation"]:
                total_trans_dyn_limit_failure+=1
            if infos[i]["yaw_dyn_lim_violation"]:
                total_yaw_dyn_limit_failure+=1
            if not is_nan_action and (infos[i]["obst_avoidance_violation"] or infos[i]["trans_dyn_lim_violation"] or infos[i]["yaw_dyn_lim_violation"]):
                total_failure+=1
        
        ##
        ## other stats
        ##

        if computation_time_verbose:
            computation_times.append(computation_time)
        costs.extend(-rews)

    return total_obs_avoidance_failure, total_trans_dyn_limit_failure, \
        total_yaw_dyn_limit_failure, total_failure, computation_times, costs, num_demos

def rollout_stats(
    trajectories: Sequence[types.TrajectoryWithRew],
) -> Mapping[str, float]:
    """Calculates various stats for a sequence of trajectories.

    Args:
        trajectories: Sequence of trajectories.

    Returns:
        Dictionary containing `n_traj` collected (int), along with episode return
        statistics (keys: `{monitor_,}return_{min,mean,std,max}`, float values)
        and trajectory length statistics (keys: `len_{min,mean,std,max}`, float
        values).

        `return_*` values are calculated from environment rewards.
        `monitor_*` values are calculated from Monitor-captured rewards, and
        are only included if the `trajectories` contain Monitor infos.
    """
    assert len(trajectories) > 0
    out_stats: Dict[str, float] = {"n_traj": len(trajectories)}
    traj_descriptors = {
        "return": np.asarray([sum(t.rews) for t in trajectories]),
        "len": np.asarray([len(t.rews) for t in trajectories]),
    }

    monitor_ep_returns = []
    for t in trajectories:
        if t.infos is not None:
            ep_return = t.infos[-1].get("episode", {}).get("r")
            if ep_return is not None:
                monitor_ep_returns.append(ep_return)
    if monitor_ep_returns:
        # Note monitor_ep_returns[i] may be from a different episode than ep_return[i]
        # since we skip episodes with None infos. This is OK as we only return summary
        # statistics, but you cannot e.g. compute the correlation between ep_return and
        # monitor_ep_returns.
        traj_descriptors["monitor_return"] = np.asarray(monitor_ep_returns)
        # monitor_return_len may be < n_traj when infos is sometimes missing
        out_stats["monitor_return_len"] = len(traj_descriptors["monitor_return"])


    stat_names = ["min", "mean", "std", "max"]
    for desc_name, desc_vals in traj_descriptors.items():
        for stat_name in stat_names:
            stat_value: np.generic = getattr(np, stat_name)(desc_vals)
            # Convert numpy type to float or int. The numpy operators always return
            # a numpy type, but we want to return type float. (int satisfies
            # float type for the purposes of static-typing).
            out_stats[f"{desc_name}_{stat_name}"] = stat_value.item()

    for v in out_stats.values():
        assert isinstance(v, (int, float))
    return out_stats, traj_descriptors


def mean_return(*args, **kwargs) -> float:
    """Find the mean return of a policy.

    Args:
        *args: Passed through to `generate_trajectories`.
        **kwargs: Passed through to `generate_trajectories`.

    Returns:
        The mean return of the generated trajectories.
    """
    trajectories = generate_trajectories(*args, **kwargs)
    return rollout_stats(trajectories)["return_mean"]


def flatten_trajectories(
    trajectories: Sequence[types.Trajectory],
) -> types.Transitions:
    """Flatten a series of trajectory dictionaries into arrays.

    Args:
        trajectories: list of trajectories.

    Returns:
        The trajectories flattened into a single batch of Transitions.
    """
    keys = ["obs", "next_obs", "acts", "dones", "infos"]
    parts = {key: [] for key in keys}
    for traj in trajectories:
        parts["acts"].append(traj.acts)

        obs = traj.obs
        parts["obs"].append(obs[:-1])
        parts["next_obs"].append(obs[1:])

        dones = np.zeros(len(traj.acts), dtype=bool)
        dones[-1] = traj.terminal
        parts["dones"].append(dones)

        if traj.infos is None:
            infos = np.array([{}] * len(traj))
        else:
            infos = traj.infos
        parts["infos"].append(infos)

    cat_parts = {
        key: np.concatenate(part_list, axis=0) for key, part_list in parts.items()
    }
    lengths = set(map(len, cat_parts.values()))
    assert len(lengths) == 1, f"expected one length, got {lengths}"
    return types.Transitions(**cat_parts)


def flatten_trajectories_with_rew(
    trajectories: Sequence[types.TrajectoryWithRew],
) -> types.TransitionsWithRew:
    transitions = flatten_trajectories(trajectories)
    rews = np.concatenate([traj.rews for traj in trajectories])
    return types.TransitionsWithRew(**dataclasses.asdict(transitions), rews=rews)


def generate_transitions(
    policy: AnyPolicy,
    venv: VecEnv,
    n_timesteps: int,
    *,
    truncate: bool = True,
    **kwargs,
) -> types.TransitionsWithRew:
    """Generate obs-action-next_obs-reward tuples.

    Args:
        policy: Can be any of the following:
            - A stable_baselines3 policy or algorithm trained on the gym environment
            - A Callable that takes an ndarray of observations and returns an ndarray
            of corresponding actions
            - None, in which case actions will be sampled randomly
        venv: The vectorized environments to interact with.
        n_timesteps: The minimum number of timesteps to sample.
        truncate: If True, then drop any additional samples to ensure that exactly
            `n_timesteps` samples are returned.
        **kwargs: Passed-through to generate_trajectories.

    Returns:
        A batch of Transitions. The length of the constituent arrays is guaranteed
        to be at least `n_timesteps` (if specified), but may be greater unless
        `truncate` is provided as we collect data until the end of each episode.
    """
    traj = generate_trajectories(
        policy,
        venv,
        sample_until=make_min_timesteps(n_timesteps),
        **kwargs,
    )
    transitions = flatten_trajectories_with_rew(traj)
    if truncate and n_timesteps is not None:
        as_dict = dataclasses.asdict(transitions)
        truncated = {k: arr[:n_timesteps] for k, arr in as_dict.items()}
        transitions = types.TransitionsWithRew(**truncated)
    return transitions

def rollout_and_save(
    path: str,
    policy: AnyPolicy,
    venv: VecEnv,
    sample_until: GenTrajTerminationFn,
    *,
    unwrap: bool = True,
    exclude_infos: bool = True,
    verbose: bool = True,
    **kwargs,
) -> None:
    """Generate policy rollouts and save them to a pickled list of trajectories.

    The `.infos` field of each Trajectory is set to `None` to save space.

    Args:
        path: Rollouts are saved to this path.
        policy: Can be any of the following:
            1) A stable_baselines3 policy or algorithm trained on the gym environment.
            2) A Callable that takes an ndarray of observations and returns an ndarray
            of corresponding actions.
            3) None, in which case actions will be sampled randomly.
        venv: The vectorized environments.
        sample_until: End condition for rollout sampling.
        unwrap: If True, then save original observations and rewards (instead of
            potentially wrapped observations and rewards) by calling
            `unwrap_traj()`.
        exclude_infos: If True, then exclude `infos` from pickle by setting
            this field to None. Excluding `infos` can save a lot of space during
            pickles.
        verbose: If True, then print out rollout stats before saving.
        **kwargs: Passed through to `generate_trajectories`.
    """
    trajs = generate_trajectories(policy, venv, sample_until, **kwargs)
    if unwrap:
        trajs = [unwrap_traj(traj) for traj in trajs]
    if exclude_infos:
        trajs = [dataclasses.replace(traj, infos=None) for traj in trajs]
    if verbose:
        stats = rollout_stats(trajs)
        logging.info(f"Rollout stats: {stats}")

    types.save(path, trajs)


def discounted_sum(arr: np.ndarray, gamma: float) -> Union[np.ndarray, float]:
    """Calculate the discounted sum of `arr`.

    If `arr` is an array of rewards, then this computes the return;
    however, it can also be used to e.g. compute discounted state
    occupancy measures.

    Args:
        arr: 1 or 2-dimensional array to compute discounted sum over.
            Last axis is timestep, from current time step (first) to
            last timestep (last). First axis (if present) is batch
            dimension.
        gamma: the discount factor used.

    Returns:
        The discounted sum over the timestep axis. The first timestep is undiscounted,
        i.e. we start at gamma^0.
    """
    # We want to calculate sum_{t = 0}^T gamma^t r_t, which can be
    # interpreted as the polynomial sum_{t = 0}^T r_t x^t
    # evaluated at x=gamma.
    # Compared to first computing all the powers of gamma, then
    # multiplying with the `arr` values and then summing, this method
    # should require fewer computations and potentially be more
    # numerically stable.
    assert arr.ndim in (1, 2)
    if gamma == 1.0:
        return arr.sum(axis=0)
    else:
        return np.polynomial.polynomial.polyval(gamma, arr)
