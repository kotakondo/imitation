"""Behavioural Cloning (BC).

Trains policy by applying supervised learning to a fixed dataset of (observation,
action) pairs generated by some expert demonstrator.
"""

import contextlib
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple, Type, Union

import gym
import numpy as np
import torch as th
import tqdm.autonotebook as tqdm
from stable_baselines3.common import policies, utils, vec_env

from imitation.algorithms import base as algo_base
from imitation.data import rollout, types
from imitation.policies import base as policy_base
from imitation.util import logger

from scipy.optimize import linear_sum_assignment
import math
import re
from compression.utils.other import getPANTHERparamsAsCppStruct


def reconstruct_policy(
    policy_path: str,
    device: Union[th.device, str] = "auto",
) -> policies.BasePolicy:
    """Reconstruct a saved policy.

    Args:
        policy_path: path where `.save_policy()` has been run.
        device: device on which to load the policy.

    Returns:
        policy: policy with reloaded weights.
    """
    policy = th.load(policy_path, map_location=utils.get_device(device))
    assert isinstance(policy, policies.BasePolicy)
    return policy


class ConstantLRSchedule:
    """A callable that returns a constant learning rate."""

    def __init__(self, lr: float = 1e-3):
        """Builds ConstantLRSchedule.

        Args:
            lr: the constant learning rate that calls to this object will return.
        """
        self.lr = lr

    def __call__(self, _):
        """Returns the constant learning rate."""
        return self.lr


class _NoopTqdm:
    """Dummy replacement for tqdm.tqdm() when we don't want a progress bar visible."""

    def close(self):
        pass

    def set_description(self, s):
        pass

    def update(self, n):
        pass


class EpochOrBatchIteratorWithProgress:
    """Wraps DataLoader so that all BC batches can be processed in one for-loop.

    Also uses `tqdm` to show progress in stdout.
    """

    def __init__(
        self,
        data_loader: Iterable[algo_base.TransitionMapping],
        n_epochs: Optional[int] = None,
        n_batches: Optional[int] = None,
        on_epoch_end: Optional[Callable[[], None]] = None,
        on_batch_end: Optional[Callable[[], None]] = None,
        progress_bar_visible: bool = True,
    ):
        """Builds EpochOrBatchIteratorWithProgress.

        Args:
            data_loader: An iterable over data dicts, as used in `BC`.
            n_epochs: The number of epochs to iterate through in one call to
                __iter__. Exactly one of `n_epochs` and `n_batches` should be provided.
            n_batches: The number of batches to iterate through in one call to
                __iter__. Exactly one of `n_epochs` and `n_batches` should be provided.
            on_epoch_end: A callback function without parameters to be called at the
                end of every epoch.
            on_batch_end: A callback function without parameters to be called at the
                end of every batch.
            progress_bar_visible: If True, then show a tqdm progress bar.

        Raises:
            ValueError: If neither or both of `n_epochs` and `n_batches` are non-None.
        """
        if n_epochs is not None and n_batches is None:
            self.use_epochs = True
        elif n_epochs is None and n_batches is not None:
            self.use_epochs = False
        else:
            raise ValueError(
                "Must provide exactly one of `n_epochs` and `n_batches` arguments.",
            )

        self.data_loader = data_loader
        self.n_epochs = n_epochs
        self.n_batches = n_batches
        self.on_epoch_end = on_epoch_end
        self.on_batch_end = on_batch_end
        self.progress_bar_visible = progress_bar_visible

    def __iter__(
        self,
    ) -> Iterable[Tuple[algo_base.TransitionMapping, Mapping[str, Any]]]:
        """Yields batches while updating tqdm display to display progress."""
        samples_so_far = 0
        epoch_num = 0
        batch_num = 0
        batch_suffix = epoch_suffix = ""
        if self.progress_bar_visible:
            if self.use_epochs:
                display = tqdm.tqdm(total=self.n_epochs)
                epoch_suffix = f"/{self.n_epochs}"
            else:  # Use batches.
                display = tqdm.tqdm(total=self.n_batches)
                batch_suffix = f"/{self.n_batches}"
        else:
            display = _NoopTqdm()

        def update_desc():
            display.set_description(
                f"batch: {batch_num}{batch_suffix}  epoch: {epoch_num}{epoch_suffix}",
            )

        with contextlib.closing(display):
            while True:
                update_desc()
                got_data_on_epoch = False
                for batch in self.data_loader:
                    got_data_on_epoch = True
                    batch_num += 1
                    batch_size = len(batch["obs"])
                    assert batch_size > 0
                    samples_so_far += batch_size
                    stats = dict(
                        epoch_num=epoch_num,
                        batch_num=batch_num,
                        samples_so_far=samples_so_far,
                    )
                    yield batch, stats
                    if self.on_batch_end is not None:
                        self.on_batch_end()
                    if not self.use_epochs:
                        update_desc()
                        display.update(1)
                        if batch_num >= self.n_batches:
                            return
                if not got_data_on_epoch:
                    raise AssertionError(
                        f"Data loader returned no data after "
                        f"{batch_num} batches, during epoch "
                        f"{epoch_num} -- did it reset correctly?",
                    )
                epoch_num += 1
                if self.on_epoch_end is not None:
                    self.on_epoch_end()

                if self.use_epochs:
                    update_desc()
                    display.update(1)
                    if epoch_num >= self.n_epochs:
                        return


class BC(algo_base.DemonstrationAlgorithm):
    """Behavioral cloning (BC).

    Recovers a policy via supervised learning from observation-action pairs.
    """

    def __init__(
        self,
        *,
        observation_space: gym.Space,
        action_space: gym.Space,
        policy: Optional[policies.BasePolicy] = None,
        demonstrations: Optional[algo_base.AnyTransitions] = None,
        batch_size: int = 32,
        evaluation_data_size: int = 1000,
        optimizer_cls: Type[th.optim.Optimizer] = th.optim.Adam,
        optimizer_kwargs: Optional[Mapping[str, Any]] = None,
        use_lr_scheduler: bool = False,
        ent_weight: float = 1e-3,
        l2_weight: float = 0.0,
        device: Union[str, th.device] = "auto",
        custom_logger: Optional[logger.HierarchicalLogger] = None,
        traj_size_pos_ctrl_pts = None,
        traj_size_yaw_ctrl_pts = None,
        use_closed_form_yaw_student = False,
        make_yaw_NN = False,
        type_loss = "Hung",
        weight_prob=0.01,
        only_test_loss=False,
        epsilon_RWTA=0.05,
    ):
        """Builds BC.

        Args:
            observation_space: the observation space of the environment.
            action_space: the action space of the environment.
            policy: a Stable Baselines3 policy; if unspecified,
                defaults to `FeedForward32Policy`.
            demonstrations: Demonstrations from an expert (optional). Transitions
                expressed directly as a `types.TransitionsMinimal` object, a sequence
                of trajectories, or an iterable of transition batches (mappings from
                keywords to arrays containing observations, etc).
            batch_size: The number of samples in each batch of expert data.
            optimizer_cls: optimiser to use for supervised training.
            optimizer_kwargs: keyword arguments, excluding learning rate and
                weight decay, for optimiser construction.
            ent_weight: scaling applied to the policy's entropy regularization.
            l2_weight: scaling applied to the policy's L2 regularization.
            device: name/identity of device to place policy on.
            custom_logger: Where to log to; if None (default), creates a new logger.

        Raises:
            ValueError: If `weight_decay` is specified in `optimizer_kwargs` (use the
                parameter `l2_weight` instead.)
        """
        self.traj_size_pos_ctrl_pts=traj_size_pos_ctrl_pts
        self.traj_size_yaw_ctrl_pts=traj_size_yaw_ctrl_pts
        self.use_closed_form_yaw_student=use_closed_form_yaw_student
        self.make_yaw_NN=make_yaw_NN
        self.type_loss=type_loss
        self.weight_prob=weight_prob
        self.batch_size = batch_size
        self.evaluation_data_size = evaluation_data_size
        self.only_test_loss = only_test_loss
        self.epsilon_RWTA = epsilon_RWTA
        self.yaw_scaling = getPANTHERparamsAsCppStruct().yaw_scaling
        self.use_lstm = getPANTHERparamsAsCppStruct().use_lstm
        self.use_lr_scheduler = use_lr_scheduler
        self.activation = {}
        
        super().__init__(
            demonstrations=demonstrations,
            custom_logger=custom_logger,
        )

        if optimizer_kwargs:
            if "weight_decay" in optimizer_kwargs:
                raise ValueError("Use the parameter l2_weight instead of weight_decay.")
        self.tensorboard_step = 0

        self.action_space = action_space
        self.observation_space = observation_space
        self.device = utils.get_device(device)

        if policy is None:
            policy = policy_base.FeedForward32Policy(
                observation_space=observation_space,
                action_space=action_space,
                # Set lr_schedule to max value to force error if policy.optimizer
                # is used by mistake (should use self.optimizer instead).
                lr_schedule=ConstantLRSchedule(th.finfo(th.float32).max),
            )

        self._policy = policy.to(self.device)
        # TODO(adam): make policy mandatory and delete observation/action space params?


        # print('policy.observation_space: ', self.policy.observation_space.shape)
        # print('self.observation_space: ', self.observation_space.shape)

        # assert self.policy.observation_space == self.observation_space  # if using lstm, this should be not matter
        assert self.policy.action_space == self.action_space

        if self.use_lr_scheduler:
            print('Use Learning Rate Scheduler')
            self.optimizer = optimizer_cls(self.policy.parameters())
            # learning rate decay occurs every mini-batch
            # self.lr_scheduler = th.optim.lr_scheduler.ExponentialLR(self.optimizer, gamma=0.9)
            self.lr_scheduler = th.optim.lr_scheduler.StepLR(self.optimizer, step_size=100, gamma=0.99)  
        else:
            print('Use Constant Learning Rate')
            self.optimizer = optimizer_cls(self.policy.parameters(), **optimizer_kwargs)

        self.ent_weight = ent_weight
        self.l2_weight = l2_weight

    @property
    def policy(self) -> policies.BasePolicy:
        return self._policy

    def set_demonstrations(self, demonstrations: algo_base.AnyTransitions) -> None:
        self._demo_data_loader = algo_base.make_data_loader(
            demonstrations,
            self.batch_size,
        )

    def set_evaluation_demonstrations(self, demonstrations: algo_base.AnyTransitions) -> None:
        self._demo_evaluation_data_loader = algo_base.make_data_loader(
            demonstrations,
            self.evaluation_data_size,
        )

    def _calculate_loss(
        self,
        obs: Union[th.Tensor, np.ndarray],
        acts: Union[th.Tensor, np.ndarray],
    ) -> Tuple[th.Tensor, Mapping[str, float]]:
        """Calculate the supervised learning loss used to train the behavioral clone.

        Args:
            obs: The observations seen by the expert. If this is a Tensor, then
                gradients are detached first before loss is calculated.
            acts: The actions taken by the expert. If this is a Tensor, then its
                gradients are detached first before loss is calculated.

        Returns:
            loss: The supervised learning loss for the behavioral clone to optimize.
            stats_dict: Statistics about the learning process to be logged.

        """
        obs = th.as_tensor(obs, device=self.device).detach()
        acts = th.as_tensor(acts, device=self.device).detach()

        if isinstance(self.policy, policies.ActorCriticPolicy):
            _, log_prob, entropy = self.policy.evaluate_actions(obs, acts)
            prob_true_act = th.exp(log_prob).mean()
            log_prob = log_prob.mean()
            entropy = entropy.mean()

            l2_norms = [th.sum(th.square(w)) for w in self.policy.parameters()]
            l2_norm = sum(l2_norms) / 2  # divide by 2 to cancel with gradient of square

            ent_loss = -self.ent_weight * entropy
            neglogp = -log_prob
            l2_loss = self.l2_weight * l2_norm
            loss = neglogp + ent_loss + l2_loss

            stats_dict = dict(
                neglogp=neglogp.item(),
                loss=loss.item(),
                entropy=entropy.item(),
                ent_loss=ent_loss.item(),
                prob_true_act=prob_true_act.item(),
                l2_norm=l2_norm.item(),
                l2_loss=l2_loss.item(),
            )

        else:
            pred_acts = self.policy.forward(obs, deterministic=True)
            # print("=====================================PRED ACTS")
            # print("pred_acts.shape= ", pred_acts.shape)
            # print("pred_acts.float()= ", pred_acts.float())
            # print("\n\n\n\n\n=====================================ACTS")
            # print("acts.shape= ", acts.shape)
            # print("acts.float()= ", acts.float())
            # loss = th.nn.MSELoss(reduction='mean')(pred_acts.float(), acts.float())
            ##########################

            used_device=acts.device

            #Expert --> i
            #Student --> j
            num_of_traj_per_action=list(acts.shape)[1] #acts.shape is [batch size, num_traj_action, size_traj]
            num_of_elements_per_traj=list(acts.shape)[2] #acts.shape is [batch size, num_traj_action, size_traj]
            batch_size=list(acts.shape)[0] #acts.shape is [batch size, num_of_traj_per_action, size_traj]

            # acts[:,:,-1]=2*(th.randint(0, 2, acts[:,:,-1].shape, device=used_device) - 0.5*th.ones(acts[:,:,-1].shape, device=used_device))
            # print(f"acts[:,:,:]=\n{acts[:,:,:]}")
            # print(f"acts[:,:,-1]=\n{acts[:,:,-1]}")

            distance_matrix= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device); 
            distance_pos_matrix= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device); 
            distance_yaw_matrix= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device); 
            distance_time_matrix= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device); 

            distance_pos_matrix_within_expert= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device); 


            for i in range(num_of_traj_per_action):
                for j in range(num_of_traj_per_action):

                    expert_i=       acts[:,i,:].float(); #All the elements
                    student_j=      pred_acts[:,j,:].float() #All the elements

                    expert_pos_i=   acts[:,i,0:self.traj_size_pos_ctrl_pts].float();
                    student_pos_j=  pred_acts[:,j,0:self.traj_size_pos_ctrl_pts].float()

                    # note expert yaw is scaled up by yaw_scaling param
                    expert_yaw_i=   acts[:,i,self.traj_size_pos_ctrl_pts:(self.traj_size_pos_ctrl_pts+self.traj_size_yaw_ctrl_pts)].float()*self.yaw_scaling
                    student_yaw_j=  pred_acts[:,j,self.traj_size_pos_ctrl_pts:(self.traj_size_pos_ctrl_pts+self.traj_size_yaw_ctrl_pts)].float()

                    expert_time_i=       acts[:,i,-1:].float(); #Time. Note: Is you use only -1 (instead of -1:), then distance_time_matrix will have required_grad to false
                    student_time_j=      pred_acts[:,j,-1:].float() #Time. Note: Is you use only -1 (instead of -1:), then distance_time_matrix will have required_grad to false

                    distance_matrix[:,i,j]=th.mean(th.nn.MSELoss(reduction='none')(expert_i, student_j), dim=1)
                    distance_pos_matrix[:,i,j]=th.mean(th.nn.MSELoss(reduction='none')(expert_pos_i, student_pos_j), dim=1)
                    distance_yaw_matrix[:,i,j]=th.mean(th.nn.MSELoss(reduction='none')(expert_yaw_i, student_yaw_j), dim=1)
                    distance_time_matrix[:,i,j]=th.mean(th.nn.MSELoss(reduction='none')(expert_time_i, student_time_j), dim=1)

                    #This is simply to delete the trajs from the expert that are repeated
                    expert_pos_j=   acts[:,j,0:self.traj_size_pos_ctrl_pts].float();
                    distance_pos_matrix_within_expert[:,i,j]=th.mean(th.nn.MSELoss(reduction='none')(expert_pos_i, expert_pos_j), dim=1)

            is_repeated=th.zeros(batch_size, num_of_traj_per_action, dtype=th.bool, device=used_device)



            for i in range(num_of_traj_per_action):
                for j in range(i+1, num_of_traj_per_action):
                    is_repeated[:,j]=th.logical_or(is_repeated[:,j], th.lt(distance_pos_matrix_within_expert[:,i,j], 1e-7))

            assert distance_matrix.requires_grad==True
            assert distance_pos_matrix.requires_grad==True
            assert distance_yaw_matrix.requires_grad==True
            assert distance_time_matrix.requires_grad==True

            #Option 1: Solve assignment problem
            A_matrix=th.ones(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device);

            #Option 2 Winner takes all
            A_WTA_matrix=th.ones(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device);
            
            distance_pos_matrix_numpy=distance_pos_matrix.cpu().detach().numpy();

            if(num_of_traj_per_action>1):

                #Option 1: Solve assignment problem
                A_matrix=th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device);

                #Option 2: Winner takes all
                # A_WTA_matrix=th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device, requires_grad=True);
                # A_WTA_matrix=distance_pos_matrix.clone();
                A_RWTAr_matrix=th.zeros_like(distance_pos_matrix)
                A_RWTAc_matrix=th.zeros_like(distance_pos_matrix)

                for index_batch in range(batch_size):         

                    # cost_matrix_numpy=distance_pos_matrix_numpy[index_batch,:,:];
                    cost_matrix=distance_pos_matrix[index_batch,:,:]
                    map2RealRows=np.array(range(num_of_traj_per_action))
                    map2RealCols=np.array(range(num_of_traj_per_action))

                    rows_to_delete=[]
                    for i in range(num_of_traj_per_action): #for each row (expert traj)
                        # expert_prob=th.round(acts[index_batch, i, -1]) #this should be either 1 or -1
                        # if(expert_prob==-1): 
                        if(is_repeated[index_batch,i]==True): 
                            #Delete that row
                            rows_to_delete.append(i)

                    # print(f"Deleting index_batch={index_batch}, rows_to_delete={rows_to_delete}")
                    # cost_matrix_numpy=np.delete(cost_matrix_numpy, rows_to_delete, axis=0)
                    cost_matrix=cost_matrix[is_repeated[index_batch,:]==False]   #np.delete(cost_matrix_numpy, rows_to_delete, axis=0)
                    cost_matrix_numpy=cost_matrix.cpu().detach().numpy()

                    #########################################################################
                    #Option 1 (Relaxed) Winner takes all 
                    #########################################################################
                    num_diff_traj_expert=cost_matrix.shape[0]
                    num_traj_student=cost_matrix.shape[1]


                    distance_pos_matrix_batch_tmp=distance_pos_matrix[index_batch,:,:].clone();
                    distance_pos_matrix_batch_tmp[is_repeated[index_batch,:],:] = float('inf')  #Set the ones that are repeated to infinity

                    ### RWTAc: This version ensures that the columns sum up to one (This is what https://arxiv.org/pdf/2110.05113.pdf does, see Eq.6)
                    minimum_per_column, row_indexes =th.min(distance_pos_matrix_batch_tmp[:,:], 0) #Select the minimum values

                    col_indexes=th.arange(0, distance_pos_matrix_batch_tmp.shape[1], dtype=th.int64)

                    if(num_diff_traj_expert>1):
                        A_RWTAc_matrix[index_batch,:,:]= (self.epsilon_RWTA/(num_diff_traj_expert-1))


                    for row_index, col_index in zip(row_indexes, col_indexes):
                        if(num_diff_traj_expert>1):
                            value=1-self.epsilon_RWTA
                        else:
                            value=1.0
                        
                        A_RWTAc_matrix[index_batch, row_index, col_index]=value

                    A_RWTAc_matrix[index_batch,is_repeated[index_batch,:],:] = th.zeros_like(A_RWTAc_matrix[index_batch,is_repeated[index_batch,:],:])

                    #assert
                    should_be_ones=th.sum(A_RWTAc_matrix[index_batch,:,:], dim=0)
                    tmp=th.isclose(should_be_ones, th.ones_like(should_be_ones))
                    assert th.all(tmp)

                    ### RWTAr: This version ensure that the non-repeated rows sum up to one
                    minimum_per_row, col_indexes =th.min(distance_pos_matrix_batch_tmp[:,:], dim=1) #Select the minimum values

                    row_indexes=th.arange(0, distance_pos_matrix_batch_tmp.shape[0], dtype=th.int64)

                    if(num_traj_student>1):
                        A_RWTAr_matrix[index_batch,:,:]= (self.epsilon_RWTA/(num_traj_student-1))


                    for row_index, col_index in zip(row_indexes, col_indexes):
                        if(num_traj_student>1):
                            value=1-self.epsilon_RWTA
                        else:
                            value=1.0
                        
                        A_RWTAr_matrix[index_batch, row_index, col_index]=value

                    A_RWTAr_matrix[index_batch,is_repeated[index_batch,:],:] = th.zeros_like(A_RWTAr_matrix[index_batch,is_repeated[index_batch,:],:])

                    #assert
                    should_be_ones=th.sum(A_RWTAr_matrix[index_batch,~is_repeated[index_batch,:],:], dim=1)
                    tmp=th.isclose(should_be_ones, th.ones_like(should_be_ones))
                    assert th.all(tmp)

                    # print("This should be zero", cost_matrix.cpu().detach().numpy() - cost_matrix_numpy)

                    map2RealRows=np.delete(map2RealRows, rows_to_delete, axis=0)


                    #########################################################################
                    #Option 2 Solve assignment problem                                       
                    #########################################################################
                    row_indexes, col_indexes = linear_sum_assignment(cost_matrix_numpy)
                    for row_index, col_index in zip(row_indexes, col_indexes):
                        A_matrix[index_batch, map2RealRows[row_index], map2RealCols[col_index]]=1
                    
            num_nonzero_A=th.count_nonzero(A_matrix); #This is the same as the number of distinct trajectories produced by the expert

            pos_loss=th.sum(A_matrix*distance_pos_matrix)/num_nonzero_A
            yaw_loss=th.sum(A_matrix*distance_yaw_matrix)/num_nonzero_A
            time_loss=th.sum(A_matrix*distance_time_matrix)/num_nonzero_A

            pos_loss_RWTAr=th.sum(A_RWTAr_matrix*distance_pos_matrix)/num_nonzero_A
            yaw_loss_RWTAr=th.sum(A_RWTAr_matrix*distance_yaw_matrix)/num_nonzero_A
            time_loss_RWTAr=th.sum(A_RWTAr_matrix*distance_time_matrix)/num_nonzero_A

            pos_loss_RWTAc=th.sum(A_RWTAc_matrix*distance_pos_matrix)/num_nonzero_A
            yaw_loss_RWTAc=th.sum(A_RWTAc_matrix*distance_yaw_matrix)/num_nonzero_A
            time_loss_RWTAc=th.sum(A_RWTAc_matrix*distance_time_matrix)/num_nonzero_A

            assert (distance_matrix.shape)[0]==batch_size, "Wrong shape!"
            assert (distance_matrix.shape)[1]==num_of_traj_per_action, "Wrong shape!"
            assert pos_loss.requires_grad==True
            assert yaw_loss.requires_grad==True
            assert time_loss.requires_grad==True

            assert pos_loss_RWTAr.requires_grad==True
            assert yaw_loss_RWTAr.requires_grad==True
            assert time_loss_RWTAr.requires_grad==True

            assert pos_loss_RWTAc.requires_grad==True
            assert yaw_loss_RWTAc.requires_grad==True
            assert time_loss_RWTAc.requires_grad==True

            loss_Hungarian = time_loss
            loss_RWTAr = time_loss_RWTAr
            loss_RWTAc = time_loss_RWTAc

            if not self.make_yaw_NN:
                loss_Hungarian += pos_loss 
                loss_RWTAr += pos_loss_RWTAr
                loss_RWTAc += pos_loss_RWTAc

            if not self.use_closed_form_yaw_student:
                loss_Hungarian += yaw_loss
                loss_RWTAr += yaw_loss_RWTAr
                loss_RWTAc += yaw_loss_RWTAc

            if(self.type_loss=="Hung"):
                loss=loss_Hungarian
            elif(self.type_loss=="RWTAr"):
                loss=loss_RWTAr
            elif(self.type_loss=="RWTAc"):
                loss=loss_RWTAc
            else:
                assert False

            stats_dict = dict(
                # loss=loss.item(),
                # loss_RWTAr=loss_RWTAr.item(),
                # loss_RWTAc=loss_RWTAc.item(),
                loss_Hungarian=loss_Hungarian.item(),
                pos_loss=pos_loss.item(),
                yaw_loss=yaw_loss.item(),
                # prob_loss=prob_loss.item(),
                time_loss=time_loss.item(),
                # percent_right_values=percent_right_values.item(),
            )

            ##
            ## Compute the loss for each trajectory 
            ##

            # tmp=th.sum(A_matrix*distance_pos_matrix, dim=2)
            # tmp[tmp==0.0]=th.nan

            # for index_batch in range(batch_size):
            #     my_sorted, indices=th.sort(tmp[index_batch,:], dim=0) #Note that if there is nans, they will be at the end of my_sorted
            #     tmp[index_batch,:]=my_sorted

            # for i in range(num_of_traj_per_action):
            #     stats_dict["pos_loss_"+str(i)]=th.nanmean(tmp[:,i]).item()

        return loss, stats_dict

    def train(
        self,
        *,
        n_epochs: Optional[int] = None,
        n_batches: Optional[int] = None,
        on_epoch_end: Callable[[], None] = None,
        on_batch_end: Callable[[], None] = None,
        log_interval: int = 500,
        log_rollouts_venv: Optional[vec_env.VecEnv] = None,
        log_rollouts_n_episodes: int = 5,
        progress_bar: bool = True,
        reset_tensorboard: bool = False,
        save_full_policy_path=None,
    ):
        """Train with supervised learning for some number of epochs.

        Here an 'epoch' is just a complete pass through the expert data loader,
        as set by `self.set_expert_data_loader()`.

        Args:
            n_epochs: Number of complete passes made through expert data before ending
                training. Provide exactly one of `n_epochs` and `n_batches`.
            n_batches: Number of batches loaded from dataset before ending training.
                Provide exactly one of `n_epochs` and `n_batches`.
            on_epoch_end: Optional callback with no parameters to run at the end of each
                epoch.
            on_batch_end: Optional callback with no parameters to run at the end of each
                batch.
            log_interval: Log stats after every log_interval batches.
            log_rollouts_venv: If not None, then this VecEnv (whose observation and
                actions spaces must match `self.observation_space` and
                `self.action_space`) is used to generate rollout stats, including
                average return and average episode length. If None, then no rollouts
                are generated.
            log_rollouts_n_episodes: Number of rollouts to generate when calculating
                rollout stats. Non-positive number disables rollouts.
            progress_bar: If True, then show a progress bar during training.
            reset_tensorboard: If True, then start plotting to Tensorboard from x=0
                even if `.train()` logged to Tensorboard previously. Has no practical
                effect if `.train()` is being called for the first time.
        """

        ##
        ## Load data
        ##

        it = EpochOrBatchIteratorWithProgress(
            self._demo_data_loader,
            n_epochs=n_epochs,
            n_batches=n_batches,
            on_epoch_end=on_epoch_end,
            on_batch_end=on_batch_end,
            progress_bar_visible=progress_bar,
        )

        ##
        ## Initialization
        ##

        if reset_tensorboard:
            self.tensorboard_step = 0
        batch_num = 0

        ##
        ## use only the final policy
        ##

        if self.only_test_loss:
            final_policy_path=re.sub(r'intermediate.*.pt', 'final_policy.pt', save_full_policy_path)   #save_full_policy_path.replace("intermediate", "final")
            print(f"Going to load policy {final_policy_path}")
            self._policy=reconstruct_policy(final_policy_path)

        ##
        ## Logging for LSTM
        ##

        if self.use_lstm:
            self.policy.lstm.register_forward_hook(self.get_activation('lstm'))

        ##
        ## Training loop
        ##

        for batch, stats_dict_it in it:

            ##
            ## training data set
            ##

            loss, stats_dict_loss = self._calculate_loss(batch["obs"], batch["acts"])

            ##
            ## Update policy
            ##

            if(self.only_test_loss==False):
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                if self.use_lr_scheduler:
                    # learning rate decay occurs every minibatch
                    self.lr_scheduler.step()

            ##
            ## Logging
            ##

            if batch_num % log_interval == 0:

                ##    
                ## training data set logger
                ##    

                for stats in [stats_dict_it, stats_dict_loss]:
                    for k, v in stats.items():
                        self.logger.record(f"bc/{k}", v)

                ##    
                ## evaluation data set logger
                ##    

                for evaluation_batch in self._demo_evaluation_data_loader:
                    _ , evaluatoin_stats_dict_loss = self._calculate_loss(evaluation_batch["obs"], evaluation_batch["acts"])
                    for k, v in evaluatoin_stats_dict_loss.items():
                        self.logger.record(f"bc/evaluation_{k}", v)

                ##
                ## LSTM Logging
                ##

                if self.use_lstm:
                    self.logger.record(f"lstm/h_norm", np.linalg.norm(self.activation['lstm'][-1]))

                ##    
                ## save policy
                ##    

                if(save_full_policy_path!=None):
                    index = save_full_policy_path.find('.pt')
                    tmp = save_full_policy_path[:index] + "_log" + str(math.floor(batch_num/log_interval)) + save_full_policy_path[index:]
                    self.save_policy(tmp)
                
                # TODO(shwang): Maybe instead use a callback that can be shared between
                #   all algorithms' `.train()` for generating rollout stats.
                #   EvalCallback could be a good fit:
                #   https://stable-baselines3.readthedocs.io/en/master/guide/callbacks.html#evalcallback
                
                ##
                ## Evaluate policy
                ##

                if log_rollouts_venv is not None and log_rollouts_n_episodes > 0:
                   
                    print("Going to evaluate student!!")
                    trajs = rollout.generate_trajectories(
                        self.policy,
                        log_rollouts_venv,
                        rollout.make_min_episodes(log_rollouts_n_episodes),
                    )
                    print("Student evaluated!!")
                    stats, traj_descriptors = rollout.rollout_stats(trajs)
                    # self.logger.record("batch_size", len(batch["obs"]))

                    ##
                    ## update learning rate
                    ##

                    print("lr_shceduler: ", self.lr_scheduler.get_last_lr())
                    self.logger.record("learning_rate", self.lr_scheduler.get_last_lr()[0])
                    
                    ##
                    ## Log stats
                    ##

                    for k, v in stats.items():
                        if "return" in k and "monitor" not in k:
                            self.logger.record("rollout/" + k, v)
                
                self.logger.dump(self.tensorboard_step)
            
            batch_num += 1
            self.tensorboard_step += 1

    def get_activation(self, name):
        """ to recoerd the lstm hidden state
        ref: test_lstm_pt.py & https://discuss.pytorch.org/t/extract-features-from-layer-of-submodule-of-a-model/20181/12"""
        self.name = name
        return self.hook

    def hook(self, model, input, output):
        """ to recoerd the lstm hidden state
        ref: test_lstm_pt.py & https://discuss.pytorch.org/t/extract-features-from-layer-of-submodule-of-a-model/20181/12"""
        self.activation[self.name] = output[0].detach().numpy()

    def save_policy(self, policy_path: types.AnyPath) -> None:
        """Save policy to a path. Can be reloaded by `.reconstruct_policy()`.

        Args:
            policy_path: path to save policy to.
        """
        th.save(self.policy, policy_path)
