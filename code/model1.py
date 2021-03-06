#!/usr/bin/env python
# coding: utf-8

# In[1]:


import os
# os.environ['TF_CPP_MIN_LOG_LEVEL']='2'
import re
import json
import math
import numpy as np
from tqdm import tqdm_notebook as tqdm

from keras_bert import load_vocabulary, load_trained_model_from_checkpoint, Tokenizer, get_checkpoint_paths

import keras.backend as K
from keras.layers import Input, Dense, Lambda, Multiply, Masking, Concatenate
from keras.models import Model
from keras.preprocessing.sequence import pad_sequences
from keras.callbacks import Callback, ModelCheckpoint
from keras.utils.data_utils import Sequence
from keras.utils import multi_gpu_model

from nl2sql.utils import read_data, read_tables, SQL, MultiSentenceTokenizer, Query, Question, Table
from nl2sql.utils.optimizer import RAdam

# import tensorflow as tf
# print('~~ gpu available: %s' % tf.test.is_available())

# ## Configuration

# In[2]:


train_table_file = '../data/train/train.tables.json'
train_data_file = '../data/train/train.json'

val_table_file = '../data/val/val.tables.json'
val_data_file = '../data/val/val.json'

test_table_file = '../data/test/test.tables.json'
test_data_file = '../data/test/test.json'

# Download pretrained BERT model from https://github.com/ymcui/Chinese-BERT-wwm
bert_model_path = '../model/chinese_wwm_L-12_H-768_A-12'
paths = get_checkpoint_paths(bert_model_path)


# ## Read Data

# In[3]:


train_tables = read_tables(train_table_file)
train_data = read_data(train_data_file, train_tables)

val_tables = read_tables(val_table_file)
val_data = read_data(val_data_file, val_tables)

test_tables = read_tables(test_table_file)
test_data = read_data(test_data_file, test_tables)
print('~~ read completed.')


# In[12]:


sample_query = train_data[0]


# In[13]:


sample_query.question


# In[14]:


sample_query.sql


# In[7]:


sample_query


# In[15]:


len(train_data), len(val_data), len(test_data)


# ## Tokenization and Label Encoding

# In[9]:


def remove_brackets(s):
    '''
    Remove brackets [] () from text
    '''
    return re.sub(r'[\(\（].*[\)\）]', '', s)

class QueryTokenizer(MultiSentenceTokenizer):
    """
    Tokenize query (question + table header) and encode to integer sequence.
    Using reserved tokens [unused11] and [unused12] for classification
    """
    
    col_type_token_dict = {'text': '[unused11]', 'real': '[unused12]'}
    
    def tokenize(self, query: Query, col_orders=None):
        """
        Tokenize quesiton and columns and concatenate.
        
        Parameters:
        query (Query): A query object contains question and table
        col_orders (list or numpy.array): For re-ordering the header columns
        
        Returns:
        token_idss: token ids for bert encoder
        segment_ids: segment ids for bert encoder
        header_ids: positions of columns
        """
        
        question_tokens = [self._token_cls] + self._tokenize(query.question.text)
        header_tokens = []
        
        if col_orders is None:
            col_orders = np.arange(len(query.table.header))
        
        header = [query.table.header[i] for i in col_orders] # 把列名混乱顺序
        
        for col_name, col_type in header:
            col_type_token = self.col_type_token_dict[col_type]
            col_name = remove_brackets(col_name)
            col_name_tokens = self._tokenize(col_name)
            col_tokens = [col_type_token] + col_name_tokens
            header_tokens.append(col_tokens)
            
        all_tokens = [question_tokens] + header_tokens
        return self._pack(*all_tokens)
    
    def encode(self, query:Query, col_orders=None):
        tokens, tokens_lens = self.tokenize(query, col_orders)
        token_ids = self._convert_tokens_to_ids(tokens)
        segment_ids = [0] * len(token_ids)
        header_indices = np.cumsum(tokens_lens)
        return token_ids, segment_ids, header_indices[:-1]


# In[10]:


token_dict = load_vocabulary(paths.vocab)
query_tokenizer = QueryTokenizer(token_dict)


# In[11]:


print('QueryTokenizer\n')
print('Input Question:\n{}\n'.format(sample_query.question))
print('Input Header:\n{}\n'.format(sample_query.table.header))
print('Output Tokens:\n{}\n'.format(' '.join(query_tokenizer.tokenize(sample_query)[0])))
print('Output token_ids:\n{}\nOutput segment_ids:\n{}\nOutput header_ids:\n{}'
      .format(*query_tokenizer.encode(sample_query)))


# In[12]:


class SqlLabelEncoder:
    """
    Convert SQL object into training labels.
    """
    def encode(self, sql: SQL, num_cols):
        cond_conn_op_label = sql.cond_conn_op
        
        sel_agg_label = np.ones(num_cols, dtype='int32') * len(SQL.agg_sql_dict)
        for col_id, agg_op in zip(sql.sel, sql.agg):
            if col_id < num_cols:
                sel_agg_label[col_id] = agg_op
            
        cond_op_label = np.ones(num_cols, dtype='int32') * len(SQL.op_sql_dict)
        # sql.conds中的元素都是长度为3的list（代表一个查询条件），
        # 第1个元素col_id是查询条件中的条件列名，第2个元素cond_op是逻辑运算符，第三个元素是条件值，这里直接不要条件值是啥意思？？？
        # 条件值在model2里面预测，看readme中的“方案介绍”部分。。。
        for col_id, cond_op, _ in sql.conds:
            if col_id < num_cols:
                cond_op_label[col_id] = cond_op
            
        return cond_conn_op_label, sel_agg_label, cond_op_label
    
    def decode(self, cond_conn_op_label, sel_agg_label, cond_op_label):
        cond_conn_op = int(cond_conn_op_label)
        sel, agg, conds = [], [], []

        for col_id, (agg_op, cond_op) in enumerate(zip(sel_agg_label, cond_op_label)):
            if agg_op < len(SQL.agg_sql_dict):
                sel.append(col_id)
                agg.append(int(agg_op))
            if cond_op < len(SQL.op_sql_dict):
                conds.append([col_id, int(cond_op)])
        return {
            'sel': sel,
            'agg': agg,
            'cond_conn_op': cond_conn_op,
            'conds': conds
        }


# In[13]:


label_encoder = SqlLabelEncoder()


# In[14]:


dict(sample_query.sql)


# In[15]:


label_encoder.encode(sample_query.sql, num_cols=len(sample_query.table.header))


# In[16]:


label_encoder.decode(*label_encoder.encode(sample_query.sql, num_cols=len(sample_query.table.header)))


# ## Build DataSequence for training

# In[17]:


class DataSequence(Sequence):
    """
    Generate training data in batches
    
    """
    def __init__(self, 
                 data, 
                 tokenizer, 
                 label_encoder, 
                 is_train=True, 
                 max_len=160, 
                 batch_size=32,
                 shuffle=True, 
                 shuffle_header=True, 
                 global_indices=None):
        
        self.data = data
        self.batch_size = batch_size
        self.tokenizer = tokenizer
        self.label_encoder = label_encoder
        self.shuffle = shuffle
        self.shuffle_header = shuffle_header
        self.is_train = is_train
        self.max_len = max_len
        
        if global_indices is None:
            self._global_indices = np.arange(len(data))
        else:
            self._global_indices = global_indices

        if shuffle:
            np.random.shuffle(self._global_indices)
    
    def _pad_sequences(self, seqs, max_len=None):
        padded = pad_sequences(seqs, maxlen=None, padding='post', truncating='post')
        if max_len is not None:
            padded = padded[:, :max_len]
        return padded
    
    def __getitem__(self, batch_id):
        batch_data_indices =             self._global_indices[batch_id * self.batch_size: (batch_id + 1) * self.batch_size]
        batch_data = [self.data[i] for i in batch_data_indices]
        
        TOKEN_IDS, SEGMENT_IDS = [], []
        HEADER_IDS, HEADER_MASK = [], []
        
        COND_CONN_OP = []
        SEL_AGG = []
        COND_OP = []
        
        for query in batch_data:
            question = query.question.text
            table = query.table
            
            col_orders = np.arange(len(table.header))
            if self.shuffle_header:
                np.random.shuffle(col_orders)

            # token_ids是把查询文本和列名（包含列的数据类型）拼接到一起，中间用预先定义的分隔符隔开。
            # header_ids是列名在token_ids中的起始位置，其长度等于列的数量。
            # segment_ids好像是全0，长度与token_ids相同（似乎是用于标记当前位置是否为分词位置的？但这里并没有用到分词，所以全0？）
            # 不管是查询文本还是列名，都视作字符序列，而不用分词。
            # 字符转化为整数形式的id，方法在keras_bert的Tokenizer类中
            token_ids, segment_ids, header_ids = self.tokenizer.encode(query, col_orders)
            header_ids = [hid for hid in header_ids if hid < self.max_len]  # 截断超长部分
            header_mask = [1] * len(header_ids) # 一个长度等于列数的全1的向量，用于构造batch填充后作为掩码
            col_orders = col_orders[: len(header_ids)]  # 跟随header_ids的长度，如果header_ids因为超长被截断一部分，这里col_orders也同样截断
            
            TOKEN_IDS.append(token_ids)
            SEGMENT_IDS.append(segment_ids)
            HEADER_IDS.append(header_ids)
            HEADER_MASK.append(header_mask)
            
            if not self.is_train:
                continue
            sql = query.sql

            # cond_conn_op是一个整数，表示查询条件之间的连接符号，0表示没有连接符号（即只有0个或1个查询条件），1表示and，2表示or（由这种表达方式可知，最多只能有2个查询条件）
            # sel_agg是一个list，长度为表的列数，这个list的元素为整数，表示各列是否出现在select子句中以及对应的聚合函数，0表示select子句中有这个列但没有聚合函数，1～5分别表示有这个列且对应聚合函数为avg、max、min、count、sum，6表示没有这个列。
            # cond_op是一个list，长度为表的列数，这个list的元素为整数，表示各列是否出现在查询条件中以及对应的逻辑运算符，0～3分别表示查询条件中有这个列且对应的逻辑运算符分别为 >、<、==、!=，4表示没有这个列
            cond_conn_op, sel_agg, cond_op = self.label_encoder.encode(sql, num_cols=len(table.header))
            
            sel_agg = sel_agg[col_orders]
            cond_op = cond_op[col_orders]
            
            COND_CONN_OP.append(cond_conn_op)
            SEL_AGG.append(sel_agg)
            COND_OP.append(cond_op)
            
        TOKEN_IDS = self._pad_sequences(TOKEN_IDS, max_len=self.max_len)
        SEGMENT_IDS = self._pad_sequences(SEGMENT_IDS, max_len=self.max_len)
        HEADER_IDS = self._pad_sequences(HEADER_IDS)
        HEADER_MASK = self._pad_sequences(HEADER_MASK)
        
        inputs = {
            'input_token_ids': TOKEN_IDS,
            'input_segment_ids': SEGMENT_IDS,
            'input_header_ids': HEADER_IDS,
            'input_header_mask': HEADER_MASK
        }
        
        if self.is_train:
            SEL_AGG = self._pad_sequences(SEL_AGG)
            SEL_AGG = np.expand_dims(SEL_AGG, axis=-1)
            COND_CONN_OP = np.expand_dims(COND_CONN_OP, axis=-1)
            COND_OP = self._pad_sequences(COND_OP)
            COND_OP = np.expand_dims(COND_OP, axis=-1)

            outputs = {
                'output_sel_agg': SEL_AGG,
                'output_cond_conn_op': COND_CONN_OP,
                'output_cond_op': COND_OP
            }
            return inputs, outputs
        else:
            return inputs
    
    def __len__(self):
        return math.ceil(len(self.data) / self.batch_size)
    
    def on_epoch_end(self):
        if self.shuffle:
            np.random.shuffle(self._global_indices)


# In[18]:


train_seq = DataSequence(train_data, query_tokenizer, label_encoder, shuffle=False, max_len=160, batch_size=32)


# In[19]:

train_seq.is_train = True
sample_batch_inputs, sample_batch_outputs = train_seq[0]
for name, data in sample_batch_inputs.items():
    print('{} : shape{}'.format(name, data.shape))
    print(data)
    
for name, data in sample_batch_outputs.items():
    print('{} : shape{}'.format(name, data.shape))
    print(data)


# ## Build Model

# In[20]:


# output sizes
num_sel_agg = len(SQL.agg_sql_dict) + 1
num_cond_op = len(SQL.op_sql_dict) + 1
num_cond_conn_op = len(SQL.conn_sql_dict)


# In[21]:


def seq_gather(x):
    seq, idxs = x
    idxs = K.cast(idxs, 'int32')
    return K.tf.batch_gather(seq, idxs)


# In[22]:


bert_model = load_trained_model_from_checkpoint(paths.config, paths.checkpoint, seq_len=None)
for l in bert_model.layers:
    l.trainable = True

# Input 这个方法似乎会默认在你指定的shape前面再加一个None的维度，比如你指定shape为(3,4)，那么实际上创建的tensor的维度为(None, 3, 4)，可能是需要默认创建batch维度
inp_token_ids = Input(shape=(None,), name='input_token_ids', dtype='int32')
inp_segment_ids = Input(shape=(None,), name='input_segment_ids', dtype='int32')
inp_header_ids = Input(shape=(None,), name='input_header_ids', dtype='int32')
inp_header_mask = Input(shape=(None, ), name='input_header_mask')

x = bert_model([inp_token_ids, inp_segment_ids]) # (None, seq_len, 768)  # x的这三个维度，None是batch维度，seq_len是序列维度，768是bert输出的embedding的长度？？？

# predict cond_conn_op。预测条件连接符
# x有三个维度，下面x[:, 0]这样的写法是对前两个维度进行索引，相当于x[:, 0, :]
# 从bert的输出序列中只取第一个元素用于条件连接符预测
x_for_cond_conn_op = Lambda(lambda x: x[:, 0])(x) # (None, 768)
p_cond_conn_op = Dense(num_cond_conn_op, activation='softmax', name='output_cond_conn_op')(x_for_cond_conn_op)

# predict sel_agg。预测查询列及聚合函数
# 下面这个这里应用seq_gather方法（其中用到batch_gather方法），使得bert输出序列中，只有对应于列名起始位置的元素被应用到预测查询列及聚合函数中，这样处理后，列名的长度（列名包含的字符数）就不再是一个变量。
x_for_header = Lambda(seq_gather, name='header_seq_gather')([x, inp_header_ids]) # (None, h_len, 768)
header_mask = Lambda(lambda x: K.expand_dims(x, axis=-1))(inp_header_mask) # (None, h_len, 1) # h_len 是列数

x_for_header = Multiply()([x_for_header, header_mask])  # 逐元素相乘
x_for_header = Masking()(x_for_header)

p_sel_agg = Dense(num_sel_agg, activation='softmax', name='output_sel_agg')(x_for_header)

# 预测条件列及逻辑运算符
x_for_cond_op = Concatenate(axis=-1)([x_for_header, p_sel_agg]) # 把预测查询列及聚合函数得到的概率，和bert输出的对应列的embedding拼接到一起
p_cond_op = Dense(num_cond_op, activation='softmax', name='output_cond_op')(x_for_cond_op)


model = Model(
    [inp_token_ids, inp_segment_ids, inp_header_ids, inp_header_mask],
    [p_cond_conn_op, p_sel_agg, p_cond_op]
)

# In[23]:

NUM_GPUS = 1
if NUM_GPUS > 1:
    print('using {} gpus'.format(NUM_GPUS))
    model = multi_gpu_model(model, gpus=NUM_GPUS)

learning_rate = 1e-5

model.compile(
    loss='sparse_categorical_crossentropy',
    optimizer=RAdam(lr=learning_rate)
)

print('~~ model.compile completed...')


# ## Training Models

# In[24]:


def outputs_to_sqls(preds_cond_conn_op, preds_sel_agg, preds_cond_op, header_lens, label_encoder):
    """
    Generate sqls from model outputs
    """
    preds_cond_conn_op = np.argmax(preds_cond_conn_op, axis=-1)
    preds_cond_op = np.argmax(preds_cond_op, axis=-1)

    sqls = []
    
    for cond_conn_op, sel_agg, cond_op, header_len in zip(preds_cond_conn_op, 
                                                          preds_sel_agg, 
                                                          preds_cond_op, 
                                                          header_lens):
        sel_agg = sel_agg[:header_len]
        # force to select at least one column for agg
        sel_agg[sel_agg == sel_agg[:, :-1].max()] = 1
        sel_agg = np.argmax(sel_agg, axis=-1)
        
        sql = label_encoder.decode(cond_conn_op, sel_agg, cond_op)
        sql['conds'] = [cond for cond in sql['conds'] if cond[0] < header_len]
        
        sel = []
        agg = []
        for col_id, agg_op in zip(sql['sel'], sql['agg']):
            if col_id < header_len:
                sel.append(col_id)
                agg.append(agg_op)
                
        sql['sel'] = sel
        sql['agg'] = agg
        sqls.append(sql)
    return sqls

class EvaluateCallback(Callback):
    def __init__(self, val_dataseq):
        self.val_dataseq = val_dataseq
    
    def on_epoch_end(self, epoch, logs=None):
        pred_sqls = []
        for batch_data in self.val_dataseq:
            header_lens = np.sum(batch_data['input_header_mask'], axis=-1)
            preds_cond_conn_op, preds_sel_agg, preds_cond_op = self.model.predict_on_batch(batch_data)
            sqls = outputs_to_sqls(preds_cond_conn_op, preds_sel_agg, preds_cond_op, 
                                   header_lens, val_dataseq.label_encoder)
            pred_sqls += sqls
            
        conn_correct = 0
        agg_correct = 0
        conds_correct = 0
        conds_col_id_correct = 0
        all_correct = 0
        num_queries = len(self.val_dataseq.data)
        
        true_sqls = [query.sql for query in self.val_dataseq.data]
        for pred_sql, true_sql in zip(pred_sqls, true_sqls):
            n_correct = 0
            if pred_sql['cond_conn_op'] == true_sql.cond_conn_op:
                conn_correct += 1
                n_correct += 1
            
            pred_aggs = set(zip(pred_sql['sel'], pred_sql['agg']))
            true_aggs = set(zip(true_sql.sel, true_sql.agg))
            if pred_aggs == true_aggs:
                agg_correct += 1
                n_correct += 1

            pred_conds = set([(cond[0], cond[1]) for cond in pred_sql['conds']])
            true_conds = set([(cond[0], cond[1]) for cond in true_sql.conds])

            if pred_conds == true_conds:
                conds_correct += 1
                n_correct += 1
   
            pred_conds_col_ids = set([cond[0] for cond in pred_sql['conds']])
            true_conds_col_ids = set([cond[0] for cond in true_sql['conds']])
            if pred_conds_col_ids == true_conds_col_ids:
                conds_col_id_correct += 1
            
            if n_correct == 3:
                all_correct += 1

        print('conn_acc: {}'.format(conn_correct / num_queries))
        print('agg_acc: {}'.format(agg_correct / num_queries))
        print('conds_acc: {}'.format(conds_correct / num_queries))
        print('conds_col_id_acc: {}'.format(conds_col_id_correct / num_queries))
        print('total_acc: {}'.format(all_correct / num_queries))
        
        logs['val_tot_acc'] = all_correct / num_queries
        logs['conn_acc'] = conn_correct / num_queries
        logs['conds_acc'] = conds_correct / num_queries
        logs['conds_col_id_acc'] = conds_col_id_correct / num_queries


# In[25]:


batch_size = NUM_GPUS * 32
num_epochs = 30

train_dataseq = DataSequence(
    data=train_data,
    tokenizer=query_tokenizer,
    label_encoder=label_encoder,
    shuffle_header=False,
    is_train=True, 
    max_len=160, 
    batch_size=batch_size
)

val_dataseq = DataSequence(
    data=val_data, 
    tokenizer=query_tokenizer,
    label_encoder=label_encoder,
    is_train=False, 
    shuffle_header=False,
    max_len=160, 
    shuffle=False,
    batch_size=batch_size
)


# In[26]:


model_path = 'task1_best_model.h5'
callbacks = [
    EvaluateCallback(val_dataseq),
    ModelCheckpoint(filepath=model_path, 
                    monitor='val_tot_acc', 
                    mode='max', 
                    save_best_only=True, 
                    save_weights_only=True)
]


# In[27]:

print('~~ model.fit_generator begin ...')
history = model.fit_generator(train_dataseq, epochs=num_epochs, callbacks=callbacks)
print('~~ model.fit_generator completed...')


# In[28]:


model.load_weights(model_path)


# ## Make prediction for task1

# In[29]:


test_dataseq = DataSequence(
    data=test_data, 
    tokenizer=query_tokenizer,
    label_encoder=label_encoder,
    is_train=False, 
    shuffle_header=False,
    max_len=160, 
    shuffle=False,
    batch_size=batch_size
)


# In[30]:


for tag, ds in [('train', train_dataseq), ('test', test_dataseq), ('val', val_dataseq)]:
    pred_sqls = []
    for batch_data in tqdm(ds):
        header_lens = np.sum(batch_data['input_header_mask'], axis=-1)
        preds_cond_conn_op, preds_sel_agg, preds_cond_op = model.predict_on_batch(batch_data)
        sqls = outputs_to_sqls(preds_cond_conn_op, preds_sel_agg, preds_cond_op,
                               header_lens, val_dataseq.label_encoder)
        pred_sqls += sqls

    # In[31]:


    task1_output_file = 'task1_output_%s.json' % tag
    with open(task1_output_file, 'w') as f:
        for sql in pred_sqls:
            json_str = json.dumps(sql, ensure_ascii=False)
            f.write(json_str + '\n')

