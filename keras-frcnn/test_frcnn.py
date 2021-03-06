from __future__ import division
import os
import cv2
import numpy as np
import sys
import pickle
from optparse import OptionParser
import time
from keras_frcnn import config
from keras import backend as K
from keras.layers import Input
from keras.models import Model
from keras_frcnn import roi_helpers
import SimpleITK as sitk


sys.setrecursionlimit(40000)

parser = OptionParser()

parser.add_option("-p", "--path", dest="test_path", help="Path to test data.")
parser.add_option("-n", "--num_rois", type="int", dest="num_rois",
				help="Number of ROIs per iteration. Higher means more memory use.", default=32)
parser.add_option("--config_filename", dest="config_filename", help=
				"Location to read the metadata related to the training (generated when training).",
				default="config.pickle")
parser.add_option("--network", dest="network", help="Base network to use. Supports vgg or resnet50.", default='resnet50')
parser.add_option("--param", dest="param_filename", help="Please specific the file of CT parameter to use", default='parameter_for_CTs.dict')
parser.add_option("--ReadFormMHD", dest="mhd_flag", help="Read image or mhd file", default=False)
parser.add_option("--Imagenet_pretrained", dest="Imagenet_pretrained", help="It is use Imagenet pre-trained weights as basebone network", default=False)
parser.add_option("--skip", dest="skip", help="detect one slice out of three", default=True)



def str_to_bool(str):
    return True if str.lower() == 'true' else False

(options, args) = parser.parse_args()


if not options.test_path:   # if filename is not given
	parser.error('Error: path to test data must be specified. Pass --path to command line')


config_output_filename = options.config_filename

with open(config_output_filename, 'rb') as f_in:
	C = pickle.load(f_in)

options.mhd_flag = str_to_bool(options.mhd_flag)
if 'Imagenet_pretrained' in C.__dict__:
	options.Imagenet_pretrained = C.Imagenet_pretrained
else:
	options.Imagenet_pretrained = str_to_bool(options.Imagenet_pretrained)

# load the CT parameter for voxel2world transformation
CT_parameter_filename = options.param_filename

with open(CT_parameter_filename, 'rb') as f_in:
	CT_parameter = pickle.load(f_in)
resultname = options.config_filename
if options.skip:
	resultname += '_skip'
submission_fd = open(resultname + '_result.csv', 'w')
submission_fd.write('seriesuid,coordX,coordY,coordZ,probability\n')


if C.network == 'resnet50':
	import keras_frcnn.resnet as nn
elif C.network == 'vgg':
	import keras_frcnn.vgg as nn
elif C.network == 'alexnet3':
	import keras_frcnn.alexnet3 as nn

# turn off any data augmentation at test time
C.use_horizontal_flips = False
C.use_vertical_flips = False
C.rot_90 = False

img_path = options.test_path

def format_img_size(img, C):
	""" formats the image size based on config """
	img_min_side = float(C.im_size)
	(height,width,_) = img.shape
		
	if width <= height:
		ratio = img_min_side/width
		new_height = int(ratio * height)
		new_width = int(img_min_side)
	else:
		ratio = img_min_side/height
		new_width = int(ratio * width)
		new_height = int(img_min_side)
	if options.mhd_flag:	
		# float32 may excess in cubic interpolation
		#img = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
		
		img = img / 255
		img = img * 1400
		img = img.astype(np.uint16)
		img = cv2.resize (img, (new_width, new_height), interpolation=cv2.INTER_LINEAR )
		img = img.astype(np.float32)
		img = img * 255
		img = img / 1400
		assert(img.dtype == np.float32)
		
	else:
		img = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_CUBIC)
	return img, ratio	

def format_img_channels(img, C):
	""" formats the image channels based on config """
	img = img[:, :, (2, 1, 0)]
	img = img.astype(np.float32)
	if 'Imagenet_pretrained' in C.__dict__:
		if C.Imagenet_pretrained == True and C.network != 'alexnet3':
			img[:, :, 0] -= C.img_channel_mean[0]
			img[:, :, 1] -= C.img_channel_mean[1]
			img[:, :, 2] -= C.img_channel_mean[2]
			img /= C.img_scaling_factor
	img = np.transpose(img, (2, 0, 1))
	img = np.expand_dims(img, axis=0)
	return img

def format_img(img, C):
	""" formats an image for model prediction based on config """
	img, ratio = format_img_size(img, C)
	img = format_img_channels(img, C)
	return img, ratio

# Method to transform the coordinates of the bounding box to its original size
def get_real_coordinates(ratio, x1, y1, x2, y2):

	real_x1 = int(round(x1 // ratio))
	real_y1 = int(round(y1 // ratio))
	real_x2 = int(round(x2 // ratio))
	real_y2 = int(round(y2 // ratio))

	return (real_x1, real_y1, real_x2 ,real_y2)

def voxel2world(zyx_, origin, spacing):

    voxel = np.array(zyx_)
    world = voxel * spacing + origin
    z, y, x = world
    z, y, x = float(z), float(y), float(x)
    
    return (z, y, x)

def load_itk(filename):
    # Reads the image using SimpleITK
    itkimage = sitk.ReadImage(filename)

    # Convert the image to a  numpy array first and then shuffle the dimensions to get axis in the order z,y,x
    ct_scan = sitk.GetArrayFromImage(itkimage)

    # Read the origin of the ct_scan, will be used to convert the coordinates from world to voxel and vice versa.
    origin = np.array(list(reversed(itkimage.GetOrigin())))

    # Read the spacing along each dimension
    spacing = np.array(list(reversed(itkimage.GetSpacing())))

    return ct_scan, origin, spacing
    
def normalizePlanes(npzarray):
    maxHU = 400.
    minHU = -1000.
 
    npzarray = (npzarray - minHU) / (maxHU - minHU)
    npzarray[npzarray>1] = 1.
    npzarray[npzarray<0] = 0.
    return npzarray


class_mapping = C.class_mapping

if 'bg' not in class_mapping:
	class_mapping['bg'] = len(class_mapping)

class_mapping = {v: k for k, v in class_mapping.items()}
print(class_mapping)
class_to_color = {class_mapping[v]: np.random.randint(0, 255, 3) for v in class_mapping}
C.num_rois = int(options.num_rois)

if C.network == 'resnet50':
	num_features = 1024
elif C.network == 'vgg':
	num_features = 512
elif C.network == 'alexnet3':
	num_features = 256

if K.image_dim_ordering() == 'th':
	input_shape_img = (3, None, None)
	input_shape_features = (num_features, None, None)
else:
	input_shape_img = (None, None, 3)
	input_shape_features = (None, None, num_features)


img_input = Input(shape=input_shape_img)
roi_input = Input(shape=(C.num_rois, 4))
feature_map_input = Input(shape=input_shape_features)

# define the base network (resnet here, can be VGG, Inception, etc)
shared_layers = nn.nn_base(img_input, trainable=True)

# define the RPN, built on the base layers
num_anchors = len(C.anchor_box_scales) * len(C.anchor_box_ratios)
rpn_layers = nn.rpn(shared_layers, num_anchors)

classifier = nn.classifier(feature_map_input, roi_input, C.num_rois, nb_classes=len(class_mapping), trainable=True)

model_rpn = Model(img_input, rpn_layers)
model_classifier_only = Model([feature_map_input, roi_input], classifier)

model_classifier = Model([feature_map_input, roi_input], classifier)

print('Loading weights from {}'.format(C.model_path))
model_rpn.load_weights(C.model_path, by_name=True)
model_classifier.load_weights(C.model_path, by_name=True)

model_rpn.compile(optimizer='sgd', loss='mse')
model_classifier.compile(optimizer='sgd', loss='mse')
model_classifier_only.compile(optimizer='sgd', loss='mse')


all_imgs = []

classes = {}

bbox_threshold = 0.8

visualise = True

for idx, img_name in enumerate(sorted(os.listdir(img_path))):
	slice_nums = 1
	if not options.mhd_flag:
		if not img_name.lower().endswith(('.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff')):
			continue
		print(img_name)
		st = time.time()
		filepath = os.path.join(img_path,img_name)

		img = cv2.imread(filepath)
		if img.ndim == 2:
			img = np.stack((img,)*3, -1)
	# reading mhd file
	else:
		if not img_name.lower().endswith('.mhd'):
			continue			
		st = time.time()
		filepath = os.path.join(img_path,img_name)
		# load the CT as a img array, the cordinator is order in z,y,x
		CT_array, origin, spacing = load_itk(filepath)
		
		slice_nums = CT_array.shape[0]

	i_skip = 0
	for iii in range(slice_nums):
		i_skip += 1
		if str_to_bool(options.skip) and i_skip % 3 != 0:
			continue
			
		if options.mhd_flag:
			img = CT_array[iii,:,:]
			img = normalizePlanes(img)
			# you should multiply 255 if you trained on ImageNet pre-trained weight 
			# for feature extraction, to get close to Imagenet data range
			if options.Imagenet_pretrained:
				img = img* 255
			img = img.astype(np.float32)
			img = np.stack((img,)*3, -1)
		

		X, ratio = format_img(img, C)

		if K.image_dim_ordering() == 'tf':
			X = np.transpose(X, (0, 2, 3, 1))

		# get the feature maps and output from the RPN
		[Y1, Y2, F] = model_rpn.predict(X)
		

		R = roi_helpers.rpn_to_roi(Y1, Y2, C, K.image_dim_ordering(), overlap_thresh=0.7)

		# convert from (x1,y1,x2,y2) to (x,y,w,h)
		R[:, 2] -= R[:, 0]
		R[:, 3] -= R[:, 1]

		# apply the spatial pyramid pooling to the proposed regions
		bboxes = {}
		probs = {}

		for jk in range(R.shape[0]//C.num_rois + 1):
			ROIs = np.expand_dims(R[C.num_rois*jk:C.num_rois*(jk+1), :], axis=0)
			if ROIs.shape[1] == 0:
				break

			if jk == R.shape[0]//C.num_rois:
				#pad R
				curr_shape = ROIs.shape
				target_shape = (curr_shape[0],C.num_rois,curr_shape[2])
				ROIs_padded = np.zeros(target_shape).astype(ROIs.dtype)
				ROIs_padded[:, :curr_shape[1], :] = ROIs
				ROIs_padded[0, curr_shape[1]:, :] = ROIs[0, 0, :]
				ROIs = ROIs_padded

			[P_cls, P_regr] = model_classifier_only.predict([F, ROIs])

			for ii in range(P_cls.shape[1]):

				if np.max(P_cls[0, ii, :]) < bbox_threshold or np.argmax(P_cls[0, ii, :]) == (P_cls.shape[2] - 1):
					continue

				cls_name = class_mapping[np.argmax(P_cls[0, ii, :])]

				if cls_name not in bboxes:
					bboxes[cls_name] = []
					probs[cls_name] = []

				(x, y, w, h) = ROIs[0, ii, :]

				cls_num = np.argmax(P_cls[0, ii, :])
				try:
					(tx, ty, tw, th) = P_regr[0, ii, 4*cls_num:4*(cls_num+1)]
					tx /= C.classifier_regr_std[0]
					ty /= C.classifier_regr_std[1]
					tw /= C.classifier_regr_std[2]
					th /= C.classifier_regr_std[3]
					x, y, w, h = roi_helpers.apply_regr(x, y, w, h, tx, ty, tw, th)
				except:
					pass
				bboxes[cls_name].append([C.rpn_stride*x, C.rpn_stride*y, C.rpn_stride*(x+w), C.rpn_stride*(y+h)])
				probs[cls_name].append(np.max(P_cls[0, ii, :]))

		all_dets = []

		for key in bboxes:
			bbox = np.array(bboxes[key])

			new_boxes, new_probs = roi_helpers.non_max_suppression_fast(bbox, np.array(probs[key]), overlap_thresh=0.5)
			for jk in range(new_boxes.shape[0]):
				(x1, y1, x2, y2) = new_boxes[jk,:]

				(real_x1, real_y1, real_x2, real_y2) = get_real_coordinates(ratio, x1, y1, x2, y2)

				cv2.rectangle(img,(real_x1, real_y1), (real_x2, real_y2), (int(class_to_color[key][0]), int(class_to_color[key][1]), int(class_to_color[key][2])),2)

				textLabel = '{}: {}'.format(key,int(100*new_probs[jk]))
				all_dets.append((key,100*new_probs[jk]))

				(retval,baseLine) = cv2.getTextSize(textLabel,cv2.FONT_HERSHEY_COMPLEX,1,1)
				textOrg = (real_x1, real_y1-0)

	            # Plot the keyname and probs
				#cv2.rectangle(img, (textOrg[0] - 5, textOrg[1]+baseLine - 5), (textOrg[0]+retval[0] + 5, textOrg[1]-retval[1] - 5), (0, 0, 0), 2)
				#cv2.rectangle(img, (textOrg[0] - 5,textOrg[1]+baseLine - 5), (textOrg[0]+retval[0] + 5, textOrg[1]-retval[1] - 5), (255, 255, 255), -1)
				#cv2.putText(img, textLabel, textOrg, cv2.FONT_HERSHEY_DUPLEX, 1, (0, 0, 0), 1)
	            #######################################
	            # save the result in submission format
	            # transform the coordinator

				
				if not options.mhd_flag:
					CT_name = img_name.partition('_')[0]
					z_v = img_name.partition('_')[2]
					z_v = int(z_v.partition('.')[0])
				else:
					z_v = iii
					CT_name = os.path.splitext(img_name)[0]
				x_v, y_v = (real_x1 + real_x2)/2, (real_y1 + real_y2)/2
				
				origin, spacing = CT_parameter[CT_name][0], CT_parameter[CT_name][1]
				
				z_w, y_w, x_w = voxel2world((z_v,y_v,x_v), origin, spacing)
				
				submission_fd.write('{},{},{},{},{}\n'.format(CT_name, x_w, y_w, z_w, new_probs[jk]))
	            #######################################
		if len(all_dets) > 0:
			print(all_dets)
		#cv2.imshow('img', img)
		#if len(all_dets) > 0:
		    #cv2.imwrite('/home/guest/alliance/keras-frcnn/results_imgs/{}.png'.format(idx),img)
		#input('any key to continue')
		# cv2.imwrite('./results_imgs/{}.png'.format(idx),img)
	
	print('Elapsed time = {}'.format(time.time() - st))
	print('processed {} CTs'.format(idx))
	
submission_fd.close()
