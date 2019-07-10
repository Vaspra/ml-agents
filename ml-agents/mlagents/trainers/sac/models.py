import logging
import numpy as np

import tensorflow as tf
from mlagents.trainers.models import LearningModel
import tensorflow.contrib.layers as c_layers

LOG_STD_MAX = 2
LOG_STD_MIN = -20
EPSILON = 1e-6  # Small value to avoid divide by zero

logger = logging.getLogger("mlagents.trainers")


class SACNetwork(LearningModel):
    def __init__(
        self,
        brain,
        m_size=None,
        h_size=128,
        normalize=False,
        use_recurrent=False,
        num_layers=2,
        stream_names=None,
        seed=0,
        is_target=False,
    ):
        LearningModel.__init__(
            self, m_size, normalize, use_recurrent, brain, seed, stream_names
        )
        self.normalize = normalize
        self.use_recurrent = use_recurrent
        self.num_layers = num_layers
        self.stream_names = stream_names
        self.h_size = h_size
        self.share_ac_cnn = False
        self.activ_fn = self.swish

        if self.use_recurrent:
            self.memory_in = tf.placeholder(
                shape=[None, self.m_size], dtype=tf.float32, name="recurrent_in"
            )

        if is_target:
            if self.use_recurrent:
                self.value_memory_in = self.memory_in
            with tf.variable_scope("target_network"):
                hidden_streams = self.create_observation_streams(
                    1, self.h_size, 0, ["critic/value/"]
                )
            if brain.vector_action_space_type == "continuous":
                self.create_cc_critic(
                    hidden_streams[0], "target_network", create_qs=False
                )
            else:
                self.create_dc_critic(
                    hidden_streams[0], "target_network", create_qs=False
                )
            if self.use_recurrent:
                mem_outs = [self.value_memory_out]
        else:
            if self.use_recurrent:
                num_mems = 4
                mem_ins = []
                for i in range(num_mems):
                    _start = self.m_size // num_mems * i
                    _end = self.m_size // num_mems * (i + 1)
                    mem_ins.append(self.memory_in[:, _start:_end])
                self.value_memory_in = mem_ins[0]
                self.q1_memory_in = mem_ins[1]
                self.q2_memory_in = mem_ins[2]
                self.policy_memory_in = mem_ins[3]

            if self.share_ac_cnn:
                with tf.variable_scope("policy_network"):
                    hidden_streams = self.create_observation_streams(
                        1, self.h_size, 0, ["critic/value/"]
                    )
                hidden_policy = hidden_streams[0]
                hidden_critic = hidden_streams[0]
            else:
                with tf.variable_scope("policy_network"):
                    hidden_streams = self.create_observation_streams(
                        2, self.h_size, 0, ["policy/", "critic/value/"]
                    )
                hidden_policy = hidden_streams[0]
                hidden_critic = hidden_streams[1]

            if brain.vector_action_space_type == "continuous":
                self.create_cc_actor(hidden_policy, "policy_network")
                self.create_cc_critic(hidden_critic, "policy_network")

            else:
                self.create_dc_actor(hidden_policy, "policy_network")
                self.create_dc_critic(hidden_critic, "policy_network")

            if self.share_ac_cnn:
                # Make sure that the policy also contains the CNN
                self.policy_vars += self.get_vars(
                    "policy_network/critic/value/main_graph_0_encoder0"
                )
            if self.use_recurrent:
                mem_outs = [
                    self.value_memory_out,
                    self.q1_memory_out,
                    self.q2_memory_out,
                    self.policy_memory_out,
                ]
        if self.use_recurrent:
            self.memory_out = tf.concat(mem_outs, axis=1, name="recurrent_out")

    def get_vars(self, scope):
        return tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope)

    def create_cc_critic(self, hidden_value, scope, create_qs=True):
        """
        Creates just the critic network
        """
        scope = scope + "/critic"
        self.value = self.create_sac_value_head(
            self.stream_names,
            hidden_value,
            self.num_layers,
            self.h_size,
            scope + "/value",
        )

        self.value_vars = self.get_vars(scope + "/value")

        if create_qs:
            hidden_q = tf.concat([hidden_value, self.external_action_in], axis=-1)
            hidden_qp = tf.concat([hidden_value, self.output_pre], axis=-1)
            self.q1_heads, self.q2_heads, self.q1, self.q2 = self.create_q_heads(
                self.stream_names, hidden_q, self.num_layers, self.h_size, scope + "/q"
            )
            self.q1_pheads, self.q2_pheads, self.q1_p, self.q2_p = self.create_q_heads(
                self.stream_names,
                hidden_qp,
                self.num_layers,
                self.h_size,
                scope + "/q",
                reuse=True,
            )
            self.q_vars = self.get_vars(scope + "/q")
        self.critic_vars = self.get_vars(scope)

    def create_dc_critic(self, hidden_value, scope, create_qs=True):
        """
        Creates just the critic network
        """
        scope = scope + "/critic"
        self.value = self.create_sac_value_head(
            self.stream_names,
            hidden_value,
            self.num_layers,
            self.h_size,
            scope + "/value",
        )

        self.value_vars = self.get_vars(scope + "/value")

        if create_qs:
            self.q1_heads, self.q2_heads, self.q1, self.q2 = self.create_q_heads(
                self.stream_names,
                hidden_value,
                self.num_layers,
                self.h_size,
                scope + "/q",
                num_outputs=sum(self.act_size),
            )
            self.q1_pheads, self.q2_pheads, self.q1_p, self.q2_p = self.create_q_heads(
                self.stream_names,
                hidden_value,
                self.num_layers,
                self.h_size,
                scope + "/q",
                reuse=True,
                num_outputs=sum(self.act_size),
            )
            self.q_vars = self.get_vars(scope)
        self.critic_vars = self.get_vars(scope)

    def create_cc_actor(self, hidden_policy, scope):
        """
        Creates Continuous control actor for SAC.
        :param hidden_policy: Output of feature extractor (i.e. the input for vector obs, output of CNN for visual obs).
        :param num_layers: TF scope to assign whatever is created in this block.
        """
        # Create action input (continuous)
        self.action_holder = tf.placeholder(
            shape=[None, self.act_size[0]], dtype=tf.float32, name="action_holder"
        )
        self.external_action_in = self.action_holder

        scope = scope + "/policy"
        hidden_policy = self.create_vector_observation_encoder(
            hidden_policy, self.h_size, self.activ_fn, self.num_layers, scope, False
        )
        # hidden_policy = tf.Print(hidden_policy,[hidden_policy], name="HiddenPoicy" )
        with tf.variable_scope(scope):
            if self.use_recurrent:  # Not sure if works
                hidden_policy, memory_out = self.create_recurrent_encoder(
                    hidden_policy, self.policy_memory_in, self.sequence_length
                )
                self.policy_memory_out = tf.identity(memory_out, name="recurrent_out")

            mu = tf.layers.dense(
                hidden_policy,
                self.act_size[0],
                activation=None,
                name="mu"
                # kernel_initializer=c_layers.variance_scaling_initializer(factor=0.01),
            )

            # Policy-dependent log_sigma_sq
            log_sigma_sq = tf.layers.dense(
                hidden_policy,
                self.act_size[0],
                activation=None,
                name="log_std"
                # kernel_initializer=c_layers.variance_scaling_initializer(factor=0.01),
            )

            self.log_sigma_sq = tf.clip_by_value(log_sigma_sq, LOG_STD_MIN, LOG_STD_MAX)

            sigma_sq = tf.exp(self.log_sigma_sq)

            # Do the reparameterization trick
            policy_ = mu + tf.random_normal(tf.shape(mu)) * sigma_sq

            _gauss_pre = -0.5 * (
                ((policy_ - mu) / (tf.exp(self.log_sigma_sq) + EPSILON)) ** 2
                + 2 * self.log_sigma_sq
                + np.log(2 * np.pi)
            )

            all_probs = tf.reduce_sum(_gauss_pre, axis=1, keepdims=True)

            self.entropy = tf.reduce_sum(
                self.log_sigma_sq + 0.5 * np.log(2.0 * np.pi * np.e), axis=-1
            )

            # Squash probabilities
            # Keep deterministic around in case we want to use it.
            self.deterministic_output_pre = tf.tanh(mu)
            self.output_pre = tf.tanh(policy_)

            # Squash correction
            all_probs -= tf.reduce_sum(
                tf.log(1 - self.output_pre ** 2 + EPSILON), axis=1, keepdims=True
            )
            # all_probs = tf.Print(all_probs, [all_probs])
            self.all_log_probs = all_probs

            self.output = tf.identity(self.output_pre, name="action")
            self.selected_actions = tf.stop_gradient(self.output_pre)

            self.action_probs = tf.identity(all_probs, name="action_probs")

        # Get all policy vars
        self.policy_vars = self.get_vars(scope)

    def create_dc_actor(self, hidden_policy, scope):
        """
        Creates Discrete control actor for SAC.
        :param hidden_policy: Output of feature extractor (i.e. the input for vector obs, output of CNN for visual obs).
        :param num_layers: TF scope to assign whatever is created in this block.
        """
        scope = scope + "/policy"
        hidden_policy = self.create_vector_observation_encoder(
            hidden_policy, self.h_size, self.activ_fn, self.num_layers, scope, False
        )
        with tf.variable_scope(scope):
            if self.use_recurrent:  # Not sure if works
                self.prev_action = tf.placeholder(
                    shape=[None, len(self.act_size)], dtype=tf.int32, name="prev_action"
                )
                prev_action_oh = tf.concat(
                    [
                        tf.one_hot(self.prev_action[:, i], self.act_size[i])
                        for i in range(len(self.act_size))
                    ],
                    axis=1,
                )
                hidden_policy = tf.concat([hidden_policy, prev_action_oh], axis=1)
                hidden_policy, memory_out = self.create_recurrent_encoder(
                    hidden_policy, self.policy_memory_in, self.sequence_length
                )
                self.policy_memory_out = tf.identity(memory_out, name="recurrent_out")

            policy_branches = []
            for size in self.act_size:
                policy_branches.append(
                    tf.layers.dense(
                        hidden_policy,
                        size,
                        activation=None,
                        use_bias=False,
                        kernel_initializer=c_layers.variance_scaling_initializer(
                            factor=0.01
                        ),
                    )
                )

            all_logits = tf.concat(
                [branch for branch in policy_branches], axis=1, name="action_probs"
            )

            # self.all_log_probs = tf.Print(self.all_log_probs, [all_probs, self.all_log_probs])

            self.action_masks = tf.placeholder(
                shape=[None, sum(self.act_size)], dtype=tf.float32, name="action_masks"
            )

            # gumbeldist = tf.contrib.distributions.RelaxedOneHotCategorical(0.05, probs=act_probs)

            output, normalized_probs, normalized_logprobs = self.create_discrete_action_masking_layer(
                all_logits, self.action_masks, self.act_size
            )

            self.action_probs = normalized_probs
            # normalized_logits = tf.Print(normalized_logits, [normalized_logits])

            # Really, this is entropy, but it has an analogous purpose to the log probs in the continuous case.
            self.all_log_probs = self.action_probs * normalized_logprobs
            # output = tf.concat(
            #     [
            #         tf.multinomial(tf.log(act_probs[k]), 1)
            #         for k in range(len(self.act_size))
            #     ],
            #     axis=1,
            # )
            self.output = output

            self.output_oh = tf.concat(
                [
                    tf.one_hot(self.output[:, i], self.act_size[i])
                    for i in range(len(self.act_size))
                ],
                axis=1,
            )
            self.selected_actions = tf.stop_gradient(self.output_oh)
            # self.output_pre = gumbeldist.sample(tf.shape(act_probs)[0])[0]
            # act_probs = tf.Print(act_probs, [act_probs, gumbeldist.sample(3)[0]])
            # self.all_log_probs = normalized_logits
            # self.all_log_probs = tf.reduce_sum(tf.log(act_probs+EPSILON), axis=1)
            # self.normalized_logits = tf.identity(normalized_logits, name="action")

            # Create action input (discrete)
            self.action_holder = tf.placeholder(
                shape=[None, len(policy_branches)], dtype=tf.int32, name="action_holder"
            )
            self.external_action_in = tf.concat(
                [
                    tf.one_hot(self.action_holder[:, i], self.act_size[i])
                    for i in range(len(self.act_size))
                ],
                axis=1,
            )

            action_idx = [0] + list(np.cumsum(self.act_size))

            # self.entropy = self.all_log_probs

            self.entropy = tf.reduce_sum(
                (
                    tf.stack(
                        [
                            tf.nn.softmax_cross_entropy_with_logits_v2(
                                labels=tf.nn.softmax(
                                    all_logits[:, action_idx[i] : action_idx[i + 1]]
                                ),
                                logits=all_logits[:, action_idx[i] : action_idx[i + 1]],
                            )
                            for i in range(len(self.act_size))
                        ],
                        axis=1,
                    )
                ),
                axis=1,
            )

            self.entropy = -self.all_log_probs

        self.policy_vars = self.get_vars(scope)

    def create_sac_value_head(
        self, stream_names, hidden_input, num_layers, h_size, scope
    ):
        """
        Creates one value estimator head for each reward signal in stream_names.
        Also creates the node corresponding to the mean of all the value heads in self.value.
        self.value_head is a dictionary of stream name to node containing the value estimator head for that signal.
        :param stream_names: The list of reward signal names
        :param hidden_input: The last layer of the Critic. The heads will consist of one dense hidden layer on top
        of the hidden input.
        :param num_layers: Number of hidden layers for value network
        :param h_size: size of hidden layers for value network
        :param scope: TF scope for value network.
        """

        value_hidden = self.create_vector_observation_encoder(
            hidden_input, h_size, self.activ_fn, num_layers, scope, False
        )
        self.value_heads = {}
        with tf.variable_scope(scope):
            if self.use_recurrent:  # Not sure if works
                value_hidden, memory_out = self.create_recurrent_encoder(
                    value_hidden, self.value_memory_in, self.sequence_length
                )
                self.value_memory_out = tf.identity(memory_out, name="recurrent_out")
            for name in stream_names:
                self.value_heads[name] = tf.layers.dense(
                    value_hidden, 1, reuse=False, name="{}_value".format(name)
                )
        value = tf.reduce_mean(list(self.value_heads.values()), 0)
        return value

    def create_q_heads(
        self,
        stream_names,
        hidden_input,
        num_layers,
        h_size,
        scope,
        reuse=False,
        num_outputs=1,
    ):
        """
        Creates two q heads for each reward signal in stream_names.
        Also creates the node corresponding to the mean of all the value heads in self.value.
        self.value_head is a dictionary of stream name to node containing the value estimator head for that signal.
        :param stream_names: The list of reward signal names
        :param hidden_input: The last layer of the Critic. The heads will consist of one dense hidden layer on top
        of the hidden input.
        :param num_layers: Number of hidden layers for Q network
        :param h_size: size of hidden layers for Q network
        :param scope: TF scope for Q network.
        :param reuse: Whether or not to reuse variables. Useful for creating Q of policy.
        :param num_outputs: Number of outputs of each Q function. If discrete, equal to number of actions. 
        """
        with tf.variable_scope(scope + "/" + "q1_encoding", reuse=reuse):
            q1_hidden = self.create_vector_observation_encoder(
                hidden_input, h_size, self.activ_fn, num_layers, "q1_encoder", reuse
            )
            if self.use_recurrent:  # Not sure if works
                q1_hidden, memory_out = self.create_recurrent_encoder(
                    q1_hidden, self.q1_memory_in, self.sequence_length
                )
                self.q1_memory_out = tf.identity(memory_out, name="recurrent_out")

            q1_heads = {}
            for name in stream_names:
                _q1 = tf.layers.dense(q1_hidden, num_outputs, name="{}_q1".format(name))
                q1_heads[name] = _q1
        with tf.variable_scope(scope + "/" + "q2_encoding", reuse=reuse):
            q2_hidden = self.create_vector_observation_encoder(
                hidden_input, h_size, self.activ_fn, num_layers, "q2_encoder", reuse
            )
            if self.use_recurrent:  # Not sure if works
                q2_hidden, memory_out = self.create_recurrent_encoder(
                    q2_hidden, self.q2_memory_in, self.sequence_length
                )
                self.q2_memory_out = tf.identity(memory_out, name="recurrent_out")

            q2_heads = {}
            for name in stream_names:
                _q2 = tf.layers.dense(q2_hidden, num_outputs, name="{}_q2".format(name))
                q2_heads[name] = _q2
        q1 = tf.reduce_mean(list(q1_heads.values()), axis=0)
        q2 = tf.reduce_mean(list(q2_heads.values()), axis=0)
        # q1 = next(iter(q1_heads.values()))
        # q2 = next(iter(q2_heads.values()))

        return q1_heads, q2_heads, q1, q2

    def copy_normalization(self, mean, variance, steps):
        update_mean = tf.assign(self.running_mean, mean)
        update_variance = tf.assign(self.running_variance, variance)
        update_norm_step = tf.assign(self.normalization_steps, steps)
        return tf.group([update_mean, update_variance, update_norm_step])


class SACModel(LearningModel):
    def __init__(
        self,
        brain,
        lr=1e-4,
        h_size=128,
        init_entcoef=0.1,
        max_step=5e6,
        normalize=False,
        use_recurrent=False,
        num_layers=2,
        m_size=None,
        seed=0,
        stream_names=None,
        tau=0.005,
        gammas=None,
    ):
        """
        Takes a Unity environment and model-specific hyper-parameters and returns the
        appropriate PPO agent model for the environment.
        :param brain: BrainInfo used to generate specific network graph.
        :param lr: Learning rate.
        :param h_size: Size of hidden layers
        :param init_entcoef: Initial value for entropy coefficient. Set lower to learn faster, set higher to explore more. .
        :return: a sub-class of PPOAgent tailored to the environment.
        :param max_step: Total number of training steps.
        :param normalize: Whether to normalize vector observation input.
        :param use_recurrent: Whether to use an LSTM layer in the network.
        :param num_layers Number of hidden layers between encoded input and policy & value layers
        :param tau Strength of soft-Q update. 
        :param m_size: Size of brain memory.
        """
        self.tau = tau
        self.gammas = gammas
        self.brain = brain
        self.init_entcoef = init_entcoef
        if stream_names is None:
            stream_names = []
        LearningModel.__init__(
            self, m_size, normalize, use_recurrent, brain, seed, stream_names
        )
        if num_layers < 1:
            num_layers = 1
        self.last_reward, self.new_reward, self.update_reward = (
            self.create_reward_encoder()
        )

        self.policy_network = SACNetwork(
            brain=brain,
            m_size=m_size,
            h_size=h_size,
            normalize=normalize,
            use_recurrent=use_recurrent,
            num_layers=num_layers,
            seed=seed,
            stream_names=stream_names,
            is_target=False,
        )
        self.target_network = SACNetwork(
            brain=brain,
            m_size=m_size // 4 if m_size else None,
            h_size=h_size,
            normalize=normalize,
            use_recurrent=use_recurrent,
            num_layers=num_layers,
            seed=seed,
            stream_names=stream_names,
            is_target=True,
        )
        self.create_inputs()
        self.create_losses(
            self.policy_network.q1_heads,
            self.policy_network.q2_heads,
            lr,
            max_step,
            stream_names,
            discrete=self.brain.vector_action_space_type == "discrete",
        )

        self.selected_actions = (
            self.policy_network.selected_actions
        )  # For GAIL and other reward signals
        if normalize:
            target_update_norm = self.target_network.copy_normalization(
                self.policy_network.running_mean,
                self.policy_network.running_variance,
                self.policy_network.normalization_steps,
            )
            self.update_normalization = tf.group(
                [self.policy_network.update_normalization, target_update_norm]
            )
            self.running_mean = self.policy_network.running_mean
            self.running_variance = self.policy_network.running_variance
            self.normalization_steps = self.policy_network.normalization_steps

    @staticmethod
    def create_reward_encoder():
        """Creates TF ops to track and increment recent average cumulative reward."""
        last_reward = tf.Variable(
            0, name="last_reward", trainable=False, dtype=tf.float32
        )
        new_reward = tf.placeholder(shape=[], dtype=tf.float32, name="new_reward")
        update_reward = tf.assign(last_reward, new_reward)
        return last_reward, new_reward, update_reward

    def create_inputs(self):
        """ 
        Assign the higher-level SACModel's inputs and outputs to those of its policy or 
        target network.
        """
        self.vector_in = self.policy_network.vector_in
        self.visual_in = self.policy_network.visual_in
        self.next_vector_in = self.target_network.vector_in
        self.next_visual_in = self.target_network.visual_in
        self.action_holder = self.policy_network.action_holder
        self.sequence_length = self.policy_network.sequence_length
        self.next_sequence_length = self.target_network.sequence_length
        if self.brain.vector_action_space_type == "discrete":
            self.action_masks = self.policy_network.action_masks

        self.output = self.policy_network.output
        self.value = self.policy_network.value
        self.value_heads = self.policy_network.value_heads
        self.all_log_probs = self.policy_network.all_log_probs
        self.dones_holder = tf.placeholder(
            shape=[None], dtype=tf.float32, name="dones_holder"
        )

        if self.use_recurrent:
            self.memory_in = self.policy_network.memory_in
            self.memory_out = self.policy_network.memory_out
            if self.brain.vector_action_space_type == "discrete":
                self.prev_action = self.policy_network.prev_action
            self.next_memory_in = self.target_network.memory_in

    def create_losses(
        self, q1_streams, q2_streams, lr, max_step, stream_names, discrete=False
    ):
        """
        Creates training-specific Tensorflow ops for SAC models.
        :param q1_streams: Q1 streams from policy network
        :param q1_streams: Q2 streams from policy network
        :param lr: Learning rate
        :param max_step: Total number of training steps.
        :param stream_names: List of reward stream names.
        :param discrete: Whether or not to use discrete action losses.
        """
        # self.min_policy_q = tf.minimum(
        #     self.policy_network.q1_p, self.policy_network.q2_p
        # )

        if discrete:
            self.target_entropy = [
                0.1 * np.log(i).astype(np.float32) for i in self.act_size
            ]
        else:
            self.target_entropy = -np.prod(self.act_size[0]).astype(np.float32)

        self.rewards_holders = {}
        self.min_policy_qs = {}

        for i, name in enumerate(stream_names):
            if discrete:
                _broken_mpq1 = self.apply_as_branches(
                    self.policy_network.q1_pheads[name]
                    * self.policy_network.action_probs
                )
                broken_mpq1 = tf.stack(
                    [tf.reduce_sum(_br, axis=1, keep_dims=True) for _br in _broken_mpq1]
                )
                _q1_p_mean = tf.reduce_mean(broken_mpq1, axis=0)

                _broken_mpq2 = self.apply_as_branches(
                    self.policy_network.q2_pheads[name]
                    * self.policy_network.action_probs
                )
                broken_mpq2 = tf.stack(
                    [tf.reduce_sum(_br, axis=1, keep_dims=True) for _br in _broken_mpq2]
                )
                _q2_p_mean = tf.reduce_mean(broken_mpq2, axis=0)

                self.min_policy_qs[name] = tf.minimum(_q1_p_mean, _q2_p_mean)

                # self.min_policy_q = tf.Print(self.min_policy_q, [self.policy_network.action_probs], summarize = 11)
            else:
                self.min_policy_qs[name] = tf.minimum(
                    self.policy_network.q1_pheads[name],
                    self.policy_network.q2_pheads[name],
                )

            rewards_holder = tf.placeholder(
                shape=[None], dtype=tf.float32, name="{}_rewards".format(name)
            )
            self.rewards_holders[name] = rewards_holder
            # self.old_values.append(old_value)
        self.learning_rate = tf.train.polynomial_decay(
            lr, self.global_step, max_step, 1e-10, power=1.0
        )

        q1_losses = []
        q2_losses = []
        # Multiple q losses per stream
        expanded_dones = tf.expand_dims(self.dones_holder, axis=-1)
        for i, name in enumerate(stream_names):
            _expanded_rewards = tf.expand_dims(self.rewards_holders[name], axis=-1)

            q_backup = tf.stop_gradient(
                _expanded_rewards
                # tf.expand_dims(self.policy_network.external_action_in[:,2] - self.policy_network.external_action_in[:,1], axis = -1)
                + (1.0 - expanded_dones) * self.gammas[i] * self.target_network.value
            )

            # q_backup = tf.Print(q_backup, [
            #     self.policy_network.external_action_in[0,2] - self.policy_network.external_action_in[0,1],
            #     (_expanded_rewards[0])
            #     ])
            if discrete:
                # We need to break up the Q functions by branch, and update them individually.
                broken_q1_stream = self.apply_as_branches(
                    self.policy_network.external_action_in * q1_streams[name]
                )
                broken_q2_stream = self.apply_as_branches(
                    self.policy_network.external_action_in * q2_streams[name]
                )

                # Reduce each branch into scalar
                broken_q1_stream = [
                    tf.reduce_sum(_branch, axis=1, keep_dims=True)
                    for _branch in broken_q1_stream
                ]
                broken_q2_stream = [
                    tf.reduce_sum(_branch, axis=1, keep_dims=True)
                    for _branch in broken_q2_stream
                ]

                q1_stream = tf.reduce_mean(broken_q1_stream, axis=0)
                q2_stream = tf.reduce_mean(broken_q2_stream, axis=0)
                # q1_stream = tf.reduce_mean(self.apply_as_branches(q1_stream), axis=0)
                # q2_stream = tf.reduce_mean(self.apply_as_branches(q2_stream), axis=0)
            else:
                q1_stream = q1_streams[name]
                q2_stream = q2_streams[name]
            # q_backup = tf.Print(q_backup, [self.policy_network.external_action_in, _expanded_rewards, q1_streams[name]], message="Qbackup", summarize=10)

            _q1_loss = 0.5 * tf.reduce_mean(
                tf.to_float(self.mask) * tf.squared_difference(q_backup, q1_stream)
            )

            _q2_loss = 0.5 * tf.reduce_mean(
                tf.to_float(self.mask) * tf.squared_difference(q_backup, q2_stream)
            )

            q1_losses.append(_q1_loss)
            q2_losses.append(_q2_loss)

        self.q1_loss = tf.reduce_mean(q1_losses)
        self.q2_loss = tf.reduce_mean(q2_losses)

        # Learn entropy coefficient
        if discrete:
            # Create a log_ent_coef for each branch
            self.log_ent_coef = tf.get_variable(
                "log_ent_coef",
                dtype=tf.float32,
                initializer=np.log([self.init_entcoef] * len(self.act_size)).astype(
                    np.float32
                ),
                trainable=True,
            )
        else:
            self.log_ent_coef = tf.get_variable(
                "log_ent_coef",
                dtype=tf.float32,
                initializer=np.log(self.init_entcoef).astype(np.float32),
                trainable=True,
            )
        self.ent_coef = tf.exp(self.log_ent_coef)
        # self.ent_coef = tf.Variable(0.2, tf.float32)

        # self.ent_coef = tf.Print(self.ent_coef, [self.ent_coef, self.policy_network.action_probs, tf.reduce_mean(tf.reduce_sum(self.policy_network.all_log_probs, axis=1, keep_dims=True))])
        if discrete:
            # We also have to do a different entropy and target_entropy per branch.
            broken_log_probs = self.apply_as_branches(self.policy_network.all_log_probs)
            broken_ent_sums = tf.stack(
                [
                    tf.reduce_sum(_lp, axis=1, keep_dims=True) + _te
                    for _lp, _te in zip(broken_log_probs, self.target_entropy)
                ],
                axis=1,
            )
            self.entropy_loss = -tf.reduce_mean(
                tf.to_float(self.mask)
                * tf.reduce_mean(
                    self.log_ent_coef * tf.squeeze(tf.stop_gradient(broken_ent_sums), axis=2),
                    axis=1,
                )
            )

            # Same with policy loss, we have to do the loss per branch and average them, so that larger branches don't get more weight.
            # The equivalent KL divergence is also pi*log(pi) - Q
            broken_q_term = self.apply_as_branches(
                self.policy_network.action_probs * self.policy_network.q1_p
            )

            broken_policy_loss = tf.stack(
                [
                    tf.reduce_sum(self.ent_coef[i] * _lp - _qt, axis=1, keep_dims=True)
                    for i, (_lp, _qt) in enumerate(zip(broken_log_probs, broken_q_term))
                ]
            )
            self.policy_loss = tf.reduce_mean(
                tf.to_float(self.mask) * tf.squeeze(broken_policy_loss)
            )

            # Do vbackup entropy bonus per branch as well.
            broken_ent_bonus = tf.stack(
                [
                    tf.reduce_sum(self.ent_coef[i] * _lp, axis=1, keep_dims=True)
                    for i, _lp in enumerate(broken_log_probs)
                ]
            )
            value_losses = []
            for name in stream_names:
                v_backup = tf.stop_gradient(
                    self.min_policy_qs[name] - tf.reduce_mean(broken_ent_bonus, axis=0)
                )
                value_losses.append(
                    0.5
                    * tf.reduce_mean(
                        tf.to_float(self.mask)
                        * tf.squared_difference(
                            self.policy_network.value_heads[name], v_backup
                        )
                    )
                )
            # v_backup = tf.Print(v_backup,[v_backup,tf.reduce_mean(broken_ent_bonus, axis=0)])
        else:
            self.entropy_loss = -tf.reduce_mean(
                self.log_ent_coef
                * tf.to_float(self.mask)
                * tf.stop_gradient(
                    tf.reduce_sum(
                        self.policy_network.all_log_probs + self.target_entropy,
                        axis=1,
                        keep_dims=True,
                    )
                )
            )
            batch_policy_loss = tf.reduce_mean(
                self.ent_coef * self.policy_network.all_log_probs
                - self.policy_network.q1_p,
                axis=1,
            )
            self.policy_loss = tf.reduce_mean(
                tf.to_float(self.mask) * batch_policy_loss
            )

            # self.policy_loss = tf.reduce_mean(- self.policy_network.q1_p)
            # self.policy_loss = tf.Print(self.policy_loss,[self.policy_network.q1_p])
            value_losses = []
            for name in stream_names:
                v_backup = tf.stop_gradient(
                    self.min_policy_qs[name]
                    - tf.reduce_sum(
                        self.ent_coef * self.policy_network.all_log_probs, axis=1
                    )
                )
                value_losses.append(
                    0.5
                    * tf.reduce_mean(
                        tf.to_float(self.mask)
                        * tf.squared_difference(
                            self.policy_network.value_heads[name], v_backup
                        )
                    )
                )
        self.value_loss = tf.reduce_mean(value_losses)

        self.total_value_loss = self.q1_loss + self.q2_loss + self.value_loss

        # Dynamic partition for masks. TODO: Figure out if this can be handled using multiplication.
        # self.total_value_loss = tf.dynamic_partition(self.total_value_loss, self.mask, 2)[1]
        # self.policy_loss = tf.dynamic_partition(self.policy_loss, self.mask, 2)[1]

        self.entropy = self.policy_network.entropy

    def apply_as_branches(self, concat_logits):
        """
        Takes in a concatenated set of logits and breaks it up into a list of non-concatenated logits, one per
        action branch
        """
        action_idx = [0] + list(np.cumsum(self.act_size))
        branches_logits = [
            concat_logits[:, action_idx[i] : action_idx[i + 1]]
            for i in range(len(self.act_size))
        ]
        return branches_logits

    def create_sac_optimizers(self):
        policy_optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        entropy_optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        value_optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)

        self.target_update_op = [
            tf.assign(target, (1 - self.tau) * target + self.tau * source)
            for target, source in zip(
                self.target_network.value_vars, self.policy_network.value_vars
            )
        ]
        print("value_vars")
        self.print_all_vars(self.policy_network.value_vars)
        print("targvalue_vars")
        self.print_all_vars(self.target_network.value_vars)
        print("critic_vars")
        self.print_all_vars(self.policy_network.critic_vars)
        print("q_vars")
        self.print_all_vars(self.policy_network.q_vars)
        print("policy_vars")
        self.print_all_vars(self.policy_network.policy_vars)

        self.target_init_op = [
            tf.assign(target, source)
            for target, source in zip(
                self.target_network.value_vars, self.policy_network.value_vars
            )
        ]

        self.update_batch_policy = policy_optimizer.minimize(
            self.policy_loss, var_list=self.policy_network.policy_vars
        )

        # Make sure policy is updated first, then value, then entropy.
        with tf.control_dependencies([self.update_batch_policy]):
            self.update_batch_value = value_optimizer.minimize(
                self.total_value_loss, var_list=self.policy_network.critic_vars
            )
            # Add entropy coefficient optimization operation
            with tf.control_dependencies([self.update_batch_value]):
                self.update_batch_entropy = entropy_optimizer.minimize(
                    self.entropy_loss, var_list=self.log_ent_coef
                )

    def print_all_vars(self, variables):
        for _var in variables:
            print(_var)
