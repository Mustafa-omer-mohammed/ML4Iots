import argparse
import numpy as np
import os
import pandas as pd
import tensorflow as tf
import tensorflow.lite as tflite
from tensorflow import keras
import zlib
import tensorflow_model_optimization as tfmot   

# Note : Python version used to excute the code is 3.7.11

######################################################### Input Parameters #########################################################
parser = argparse.ArgumentParser()
parser.add_argument('--version', type=str, required=True, help=' version to be excuted choose from [a,b,c] ')
args = parser.parse_args()

version = args.version
################## Fix the Random seed to reproduce the same results 
seed = 42
tf.random.set_seed(seed)
np.random.seed(seed)

units = 8   # fix the units to number of class labels = 8 [8: stop words without silence , 9: with silence]

######################################################## Options for version a
if version == "a" :
    m = "ds_cnn"   # model name [ mlp , cnn , ds_cnn  ]
    alpha = 0.5    # The width multiplier used to apply the structured Pruning 
    mfcc = True    # True --> excute mfcc , False --> excute STFT

    MFCC_OPTIONS = {'frame_length': 640, 'frame_step': 320, 'mfcc': True,'lower_frequency': 20, 'upper_frequency': 4000 , 'num_mel_bins': 40,'num_coefficients': 10}

 ######################################################## Options for version b   
if version == "b" :
    m = "cnn"   # model name [ mlp , cnn , ds_cnn  ]
    alpha = 0.4    # The width multiplier used to apply the structured Pruning 
    mfcc = True    # True --> excute mfcc , False --> excute STFT
    MFCC_OPTIONS = {'frame_length': 1024, 'frame_step': 400, 'mfcc': True,  'lower_frequency': 20, 'upper_frequency': 4000, 'num_mel_bins': 16, 'num_coefficients': 10}

######################################################## Options for version c
if version == "c" :
    m = "ds_cnn"   # model name [ mlp , cnn , ds_cnn  ]
    alpha = 0.3    # The width multiplier used to apply the structured Pruning 
    mfcc = True    # True --> excute mfcc , False --> excute STFT
    MFCC_OPTIONS = {'frame_length': 1024, 'frame_step': 400, 'mfcc': True,  'lower_frequency': 20, 'upper_frequency': 4000, 'num_mel_bins': 16, 'num_coefficients': 10}


STFT_OPTIONS = {'frame_length': 256, 'frame_step': 128, 'mfcc': False}  # always achieved low performance results 

model_version = f"_V_{version}_alpha={alpha}"

mymodel = m + model_version
TFLITE =  f'Group26_kws_{version}.tflite'                                 # path for saving the best model after converted to TF.lite model 


if mfcc is True:
    options = MFCC_OPTIONS
    strides = [2, 1]
else:
    options = STFT_OPTIONS
    strides = [2, 2]

########################################################
print(f"EXCUTING MODEL {mymodel}")


######################################################## Reading the data and split to Train , Validation and Test #########################################################

zip_path = tf.keras.utils.get_file(
    origin="http://storage.googleapis.com/download.tensorflow.org/data/mini_speech_commands.zip",
    fname='mini_speech_commands.zip',
    extract=True,
    cache_dir='.', cache_subdir='data')

data_dir = os.path.join('.', 'data', 'mini_speech_commands')

############## Using the splits provided by the text of the assignment 
train_files = tf.convert_to_tensor(np.loadtxt("kws_train_split.txt" , dtype = str ))
val_files = tf.convert_to_tensor(np.loadtxt("kws_val_split.txt" , dtype = str ) )
test_files = tf.convert_to_tensor(np.loadtxt("kws_test_split.txt" , dtype = str ))


# with silence ['stop', 'up', 'yes', 'right', 'left', 'no', 'silence', 'down', 'go']
LABELS = np.array(['stop', 'up', 'yes', 'right', 'left', 'no',  'down', 'go'] , dtype = str) 
print (f"The LABELS order as provided to the model are {LABELS}")


######################################################## Create the SignalGenerator #########################################################


class SignalGenerator:
    def __init__(self, labels, sampling_rate, frame_length, frame_step,
            num_mel_bins=None, lower_frequency=None, upper_frequency=None,
            num_coefficients=None, mfcc=False):
        self.labels = labels
        self.sampling_rate = sampling_rate                                             # 16000  
        self.frame_length = frame_length                                               # 640 
        self.frame_step = frame_step                                                   # 320 
        self.num_mel_bins = num_mel_bins                                               # 40 
        self.lower_frequency = lower_frequency                                         # 20 
        self.upper_frequency = upper_frequency                                         # 4000
        self.num_coefficients = num_coefficients                                       # 10 
        num_spectrogram_bins = (frame_length) // 2 + 1                                  # ( frame size // 2 ) + 1 

   

        if mfcc is True:                                          # to speed up the preprocessing we need to compute the linear_to_mel_weight_matrix once so it will be a class argument 
            self.linear_to_mel_weight_matrix = tf.signal.linear_to_mel_weight_matrix(
                    self.num_mel_bins, num_spectrogram_bins, self.sampling_rate,
                    self.lower_frequency, self.upper_frequency)
            self.preprocess = self.preprocess_with_mfcc
        else:
            self.preprocess = self.preprocess_with_stft

    def read(self, file_path):
        parts = tf.strings.split(file_path,  "/")
        label = parts[-2]                                 
        label_id = tf.argmax(label == self.labels)        # extract the label ID (the integer mapping of the label)
        audio_binary = tf.io.read_file(file_path)         # reading the audio file in byte format
        audio, _ = tf.audio.decode_wav(audio_binary)      # decode a 16-bit PCM WAV file to a float tensor
        audio = tf.squeeze(audio, axis=1)

        return audio, label_id

    def pad(self, audio):
        # Padding for files with length less than 16000 samples
        zero_padding = tf.zeros([self.sampling_rate] - tf.shape(audio), dtype=tf.float32)     # if the shape of the audio is already = 16000 (sampling rate) we will add nothing 

        # Concatenate audio with padding so that all audio clips will be of the same length
        audio = tf.concat([audio, zero_padding], 0)
        # Unify the shape to the sampling frequency (16000 , )
        audio.set_shape([self.sampling_rate])

        return audio

    def get_spectrogram(self, audio):
        stft = tf.signal.stft(audio, frame_length=self.frame_length,
                frame_step=self.frame_step, fft_length=self.frame_length)
        spectrogram = tf.abs(stft)

        return spectrogram

    def get_mfccs(self, spectrogram):
        mel_spectrogram = tf.tensordot(spectrogram,
                self.linear_to_mel_weight_matrix, 1)
        log_mel_spectrogram = tf.math.log(mel_spectrogram + 1.e-6)
        mfccs = tf.signal.mfccs_from_log_mel_spectrograms(log_mel_spectrogram)
        mfccs = mfccs[..., :self.num_coefficients]

        return mfccs

    def preprocess_with_stft(self, file_path):
        audio, label = self.read(file_path)
        audio = self.pad(audio)
        spectrogram = self.get_spectrogram(audio)
        spectrogram = tf.expand_dims(spectrogram, -1)                         # expand_dims will not add or reduce elements in a tensor, it just changes the shape by adding 1 to dimensions for the batchs. 
    
        spectrogram = tf.image.resize(spectrogram, [32, 32])

        return spectrogram, label

    def preprocess_with_mfcc(self, file_path):
        audio, label = self.read(file_path)
        audio = self.pad(audio)
        spectrogram = self.get_spectrogram(audio)
        mfccs = self.get_mfccs(spectrogram)
        mfccs = tf.expand_dims(mfccs, -1)

        return mfccs, label

    def make_dataset(self, files, train):
        ds = tf.data.Dataset.from_tensor_slices(files)
        ds = ds.map(self.preprocess, num_parallel_calls = tf.data.experimental.AUTOTUNE) # parallel mapping exploiting the best number of parallel workers 
        ds = ds.batch(32)                                                                # create batches of 32 samples
        ds = ds.cache()                                                                  # cashe is used to avoid recomputing the previous preprocessing
        ds = ds.prefetch(tf.data.experimental.AUTOTUNE)                                  # applied to start reading the next batch from memory while prpcessing the current one
        if train is True:
            ds = ds.shuffle(100, reshuffle_each_iteration=True)

        return ds
######################################################## Generate Data set splits #########################################################

generator = SignalGenerator(LABELS, 16000, **options)
train_ds = generator.make_dataset(train_files, True)
val_ds = generator.make_dataset(val_files, False)
test_ds = generator.make_dataset(test_files, False)

########################################################  building the models ########################################################
cnn = tf.keras.Sequential([
    tf.keras.layers.Conv2D(filters=int(128 *alpha), kernel_size=[3,3], strides=strides, use_bias=False , name = "Conv2D-1"),
    tf.keras.layers.BatchNormalization(momentum=0.1 , name = "Btch_Norm-1"),
    tf.keras.layers.ReLU(),
    tf.keras.layers.Conv2D(filters=int(128 *alpha), kernel_size=[3,3], strides=[1,1], use_bias=False , name = "Conv2D-2"),
    tf.keras.layers.BatchNormalization(momentum=0.1 , name = "Btch_Norm-2"),
    tf.keras.layers.ReLU(),
    tf.keras.layers.Conv2D(filters=int(128 *alpha), kernel_size=[3,3], strides=[1,1], use_bias=False , name = "Conv2D-3"),
    tf.keras.layers.BatchNormalization(momentum=0.1 , name = "Btch_Norm-3"),
    tf.keras.layers.ReLU(),
    tf.keras.layers.GlobalAveragePooling2D( name =  "GlobalAveragePooling-Layer"),
    tf.keras.layers.Dense(units = units, name =  "Output-Layer")
])

ds_cnn = tf.keras.Sequential([
    tf.keras.layers.Conv2D(filters=int(256 *alpha), kernel_size=[3,3], strides=strides, use_bias=False, name = "Conv2D-1"),
    tf.keras.layers.BatchNormalization(momentum=0.1),
    tf.keras.layers.ReLU(),
    tf.keras.layers.DepthwiseConv2D(kernel_size=[3, 3], strides=[1, 1], use_bias=False, name = "DepthwiseConv2D-1"),
    tf.keras.layers.Conv2D(filters=int(256 *alpha), kernel_size=[1,1], strides=[1,1], use_bias=False, name = "Conv2D-2"),
    tf.keras.layers.BatchNormalization(momentum=0.1),
    tf.keras.layers.ReLU(),
    tf.keras.layers.DepthwiseConv2D(kernel_size=[3, 3], strides=[1, 1], use_bias=False, name = "DepthwiseConv2D-2"),
    tf.keras.layers.Conv2D(filters=int(256 *alpha), kernel_size=[1,1], strides=[1,1], use_bias=False, name = "Conv2D-3"),
    tf.keras.layers.BatchNormalization(momentum=0.1),
    tf.keras.layers.ReLU(),
    tf.keras.layers.GlobalAveragePooling2D( name =  "GlobalAveragePooling-Layer"),
    tf.keras.layers.Dense(units = units, name =  "Output-Layer")
])


MODELS = {'cnn'+ model_version: cnn, 'ds_cnn'+ model_version: ds_cnn}
# print(MODELS.keys())

######################################################## Define optimizer & Losses & Metrics ########################################################


model = MODELS[mymodel]              # initiate the selected model 

loss = tf.losses.SparseCategoricalCrossentropy(from_logits=True)
optimizer = tf.optimizers.Adam()
metrics = [tf.keras.metrics.SparseCategoricalAccuracy()]


################### Compiling the model :

model.compile(loss = loss, optimizer = optimizer, metrics = metrics)

######################################################## check points depending on preprocessing STFT , MFCC 
if mfcc is False:
    checkpoint_filepath = f'./checkpoints/stft/chkp_best_{mymodel}'

else:
    checkpoint_filepath = f'./checkpoints/mfcc/chkp_best_{mymodel}'
    
model_checkpoint_callback = tf.keras.callbacks.ModelCheckpoint(
    filepath=checkpoint_filepath,           
    monitor='val_sparse_categorical_accuracy',
    verbose=1,
    mode='max',
    save_best_only=True,
    save_freq='epoch')
######################################################## Model Training ########################################################

history = model.fit(train_ds, epochs=20,   validation_data=val_ds,callbacks=[model_checkpoint_callback ])

############################## Print Model Summary ####################
print(model.summary())    

############################## Plot The training and validation losses ####################
# import matplotlib.pyplot as plt
# def plot_loss(history):
#     plt.plot(history.history['sparse_categorical_accuracy'], label='Accuracy')
#     plt.plot(history.history['val_sparse_categorical_accuracy'], label='val_Accuracy')
#     plt.xlabel('Epoch')
#     plt.ylabel('Accuracy')
#     plt.legend()
#     plt.grid(True)
#     plt.savefig(mymodel+".png")

# plot_loss(history)

########################################################  Evaluate the best model on test data  ########################################################

best_model = tf.keras.models.load_model(filepath = checkpoint_filepath )
Loss , ACCURACY = best_model.evaluate(test_ds)
print("*"*50,"\n",f" The accuracy achieved by the best model before convertion = {ACCURACY *100:0.2f}% ")
# Function for weight and activations quantization 
def representative_dataset_gen():
    for x, _ in train_ds.take(1000):
        yield [x]    
    
########################################################  Structured Pruning + Quantization  ########################################################

def S_pruning_Model_evaluate_and_compress_to_TFlite(tflite_model_dir =  TFLITE , without_Q = False,  PQT = False , WAPQT = False ,  checkpoint_filepath = checkpoint_filepath ):
    if not os.path.exists('./models'):
        os.makedirs('./models')   
    
    converter = tf.lite.TFLiteConverter.from_saved_model(checkpoint_filepath)

     # Convert to TF lite without Quantization 
    if without_Q == True :   
        tflite_model = converter.convert()  
        Compressed = f"{tflite_model_dir}.zlib"
        tflite_model_dir = './models/'+tflite_model_dir
        # Write the model in binary formate and save it 
        with open(tflite_model_dir, 'wb') as fp:
            fp.write(tflite_model)
        Compressed = './models/'+Compressed
        with open(Compressed, 'wb') as fp:
            tflite_compressed = zlib.compress(tflite_model)
            fp.write(tflite_compressed)
        print("*"*50,"\n",f"the model is saved successfuly to {tflite_model_dir}")
        return Compressed , tflite_model_dir 
    else:
        # Apply weight only quantization 
        if PQT == True :
            converter.optimizations = [tf.lite.Optimize.DEFAULT]
            tflite_model = converter.convert()
        # Apply weight + Activation  quantization 
        if WAPQT == True :
            converter.optimizations = [tf.lite.Optimize.DEFAULT]
            converter.representative_dataset = representative_dataset_gen
            tflite_model = converter.convert()
            
        Compressed =  f"{tflite_model_dir}.zlib"
        tflite_model_dir =   f"./models/{tflite_model_dir}"
        # Write the model in binary formate and save it 
        with open(tflite_model_dir, 'wb') as fp:
            fp.write(tflite_model)
        Compressed = f"./models/{Compressed}"
        with open(Compressed, 'wb') as fp:
            tflite_compressed = zlib.compress(tflite_model)
            fp.write(tflite_compressed)
        print(f"the model is saved successfuly to {tflite_model_dir}")
        return Compressed , tflite_model_dir 
######################################################## Quantization aware Training ########################################################

Q_aware_checkpoint_filepath = F'Q_aware_chkp_best_{mymodel}'
    
Q_aware_model_checkpoint_callback = tf.keras.callbacks.ModelCheckpoint(
    filepath=Q_aware_checkpoint_filepath,           
    monitor='val_sparse_categorical_accuracy',
    verbose=1,
    mode='max',
    save_best_only=True,
    save_freq='epoch')
def Quantization_aware_traning(filepath = checkpoint_filepath , checkpoint_callback = Q_aware_model_checkpoint_callback ):

    quantize_model = tfmot.quantization.keras.quantize_model
    
    # Retrieve the best pre_trained model float 32 
    model = tf.keras.models.load_model(filepath = filepath )
    
    # Initiate a Quantization aware model from the Float 32 model to be trained 
    q_aware_model = quantize_model(model)
    
    # Model compile and define loss and metric 
    q_aware_model.compile(loss = loss, optimizer = optimizer, metrics = metrics)
    
    # Train the model for few epochs 
    q_aware_model_history = q_aware_model.fit(train_ds, epochs=10,   validation_data=val_ds,callbacks=[checkpoint_callback ])
    
    ############################## Print Model Summary ####################
    print(q_aware_model.summary())
    
    ############################## Evaluate the best model  #################### 
    best_model = tf.keras.models.load_model(filepath = Q_aware_checkpoint_filepath )
    Loss , ACCURACY = best_model.evaluate(test_ds)
    print("*"*50,"\n",f" The accuracy achieved by the best model before convertion = {ACCURACY *100:0.2f}% ")
######################################################## Quantization Aware model saving ########################################################
def Q_Aware_T_Tflite_save(filepath = Q_aware_checkpoint_filepath):
    if not os.path.exists('./models'):
        os.makedirs('./models')
    converter = tf.lite.TFLiteConverter.from_saved_model(filepath)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    tflite_model = converter.convert()
    Compressed = F"{TFLITE}.zlib"
    QAT_tflite_model_dir = './models/'+TFLITE
    # Write the model in binary formate and save it 
    with open(QAT_tflite_model_dir, 'wb') as fp:
        fp.write(tflite_model)
    Compressed = './models/'+Compressed
    with open(Compressed, 'wb') as fp:
        tflite_compressed = zlib.compress(tflite_model)
        fp.write(tflite_compressed)
    print("*"*50,"\n",f"the model is saved successfuly to {QAT_tflite_model_dir}")
    return QAT_tflite_model_dir , Compressed
########################################################  Execute version A :
if version == "a" :
    # convert to Tf lite and apply Post Trianing Quantization with weights only :
    Compressed , Quantized  = S_pruning_Model_evaluate_and_compress_to_TFlite(tflite_model_dir =  TFLITE ,  PQT = True)
    
    # Evaluate the Tflite model 
    load_and_evaluation(Quantized , test_ds , Compressed)

if version == "b" :
    # apply quantization aware Trainig before quantization  :
    Quantization_aware_traning(filepath = checkpoint_filepath , checkpoint_callback = Q_aware_model_checkpoint_callback )
    # convert to Tf lite and apply Post Trianing Quantization  :
    QAT_tflite_model_dir , Q_Aware_T_Compressed = Q_Aware_T_Tflite_save(filepath = Q_aware_checkpoint_filepath)
    # Evaluate the Tflite model 
    load_and_evaluation(QAT_tflite_model_dir, test_ds , Q_Aware_T_Compressed)



if version == "c" :
    # convert to Tf lite and apply Post Trianing Quantization with weights only :
    Compressed , Quantized  = S_pruning_Model_evaluate_and_compress_to_TFlite(tflite_model_dir =  TFLITE ,  PQT = True)
    
    # Evaluate the Tflite model 
    load_and_evaluation(Quantized , test_ds , Compressed)
