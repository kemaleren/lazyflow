import numpy, vigra, h5py
import traceback
from lazyflow.graph import Operator, InputSlot, OutputSlot, OrderedSignal
from lazyflow import roi
from lazyflow.roi import sliceToRoi
import copy
from lazyflow.request import Pool

from operators import OpArrayPiper
from lazyflow.rtype import SubRegion

import os
from collections import deque

from generic import OpMultiArrayStacker, popFlagsFromTheKey

import math

from threading import Lock
from lazyflow.roi import roiToSlice
from functools import partial

import logging
logger = logging.getLogger(__name__)

def zfill_num(n, stop):
    """ Make int strings same length.

    >>> zfill_num(1, 100) # len('99') == 2
    '01'

    >>> zfill_num(1, 101) # len('100') == 3
    '001'

    """
    return str(n).zfill(len(str(stop - 1)))


def makeOpXToMulti(n):
    """A factory for creating OpXToMulti classes."""
    assert n > 0

    class OpXToMulti(Operator):
        category = "Misc"
        name = "{} Element to Multislot".format(n)

        if n == 1:
            inputSlots = [InputSlot('Input')]
        else:
            names = list("Input{}".format(zfill_num(i, n))
                         for i in range(n))
            inputSlots = list(InputSlot(name, optional=True)
                                   for name in names)

        outputSlots = [OutputSlot("Outputs", level=1)]

        def _sorted_inputs(self, filterReady=False):
            """Returns self.inputs.values() sorted by keys.

            :param filterReady: only return slots that are ready.

            """
            keys = sorted(self.inputs.keys())
            slots = list(self.inputs[k] for k in keys)
            if filterReady:
                slots = list(s for s in slots if s.ready())
            return slots

        def _do_assignfrom(self, inslots):
            for inslot, outslot in zip(inslots, self.outputs['Outputs']):
                outslot.meta.assignFrom(inslot.meta)

        def setupOutputs(self):
            inslots = self._sorted_inputs(filterReady=True)
            self.outputs["Outputs"].resize(len(inslots))
            self._do_assignfrom(inslots)

        def execute(self, slot, subindex, roi, result):
            key = roiToSlice(roi.start, roi.stop)
            index = subindex[0]
            inslots = self._sorted_inputs(filterReady=True)
            if index < len(inslots):
                return inslots[index][key].allocate().wait()

        def propagateDirty(self, islot, subindex, roi):
            inslots = self._sorted_inputs()
            index = inslots.index(islot)
            self.outputs["Outputs"][index].setDirty(roi)
            readyslots = list(s for s in inslots[:index] if s.ready())
            self._do_assignfrom(readyslots)

        def setInSlot(self, slot, subindex, roi, value):
            # Nothing to do here: All inputs are directly connected to an input slot.
            pass

    return OpXToMulti


Op1ToMulti = makeOpXToMulti(1)
Op5ToMulti = makeOpXToMulti(5)
Op10ToMulti = makeOpXToMulti(10)
Op20ToMulti = makeOpXToMulti(20)
Op50ToMulti = makeOpXToMulti(50)


class OpPixelFeaturesPresmoothed(Operator):
    name="OpPixelFeaturesPresmoothed"
    category = "Vigra filter"

    inputSlots = [InputSlot("Input"),
                  InputSlot("Matrix"),
                  InputSlot("Scales"),
                  InputSlot("FeatureIds")] # The selection of features to compute

    outputSlots = [OutputSlot("Output"),        # The entire block of features as a single image (many channels)
                   OutputSlot("Features", level=1)] # Each feature image listed separately, with feature name provided in metadata

    # Specify a default set & order for the features we compute
    DefaultFeatureIds = [ 'GaussianSmoothing',
                          'LaplacianOfGaussian',
                          'StructureTensorEigenvalues',
                          'HessianOfGaussianEigenvalues',
                          'GaussianGradientMagnitude',
                          'DifferenceOfGaussians' ]

    def __init__(self, *args, **kwargs):
        Operator.__init__(self, *args, **kwargs)
        self.source = OpArrayPiper(parent=self)
        self.source.inputs["Input"].connect(self.inputs["Input"])

        self.stacker = OpMultiArrayStacker(parent=self)

        self.multi = Op50ToMulti(parent=self)

        self.stacker.inputs["Images"].connect(self.multi.outputs["Outputs"])

        # Give our feature IDs input a default value (connected out of the box, but can be changed)
        self.inputs["FeatureIds"].setValue( self.DefaultFeatureIds )

    def setupOutputs(self):
        if self.inputs["Scales"].connected() and self.inputs["Matrix"].connected():

            self.stacker.inputs["Images"].disconnect()
            self.scales = self.inputs["Scales"].value
            self.matrix = self.inputs["Matrix"].value

            if not isinstance(self.matrix, numpy.ndarray):
                raise RuntimeError("OpPixelFeatures: Please input a numpy.ndarray as 'Matrix'")

            dimCol = len(self.scales)
            dimRow = len(self.inputs["FeatureIds"].value)

            assert dimRow== self.matrix.shape[0], "Please check the matrix or the scales they are not the same (scales = %r, matrix.shape = %r)" % (self.scales, self.matrix.shape)
            assert dimCol== self.matrix.shape[1], "Please check the matrix or the scales they are not the same (scales = %r, matrix.shape = %r)" % (self.scales, self.matrix.shape)

            featureNameArray =[]
            oparray = []
            for j in range(dimRow):
                oparray.append([])
                featureNameArray.append([])

            self.newScales = []
            for j in range(dimCol):
                destSigma = 1.0
                if self.scales[j] > destSigma:
                    self.newScales.append(destSigma)
                else:
                    self.newScales.append(self.scales[j])

                logger.debug("Replacing scale %f with new scale %f" %(self.scales[j], self.newScales[j]))

            for i, featureId in enumerate(self.inputs["FeatureIds"].value):
                if featureId == 'GaussianSmoothing':
                    for j in range(dimCol):
                        oparray[i].append(OpGaussianSmoothing(self))
                        oparray[i][j].inputs["Input"].connect(self.source.outputs["Output"])
                        oparray[i][j].inputs["sigma"].setValue(self.newScales[j])
                        featureNameArray[i].append("Gaussian Smoothing (s=" + str(self.scales[j]) + ")")

                elif featureId == 'LaplacianOfGaussian':
                    for j in range(dimCol):
                        oparray[i].append(OpLaplacianOfGaussian(self))
                        oparray[i][j].inputs["Input"].connect(self.source.outputs["Output"])
                        oparray[i][j].inputs["scale"].setValue(self.newScales[j])
                        featureNameArray[i].append("Laplacian of Gaussian (s=" + str(self.scales[j]) + ")")

                elif featureId == 'StructureTensorEigenvalues':
                    for j in range(dimCol):
                        oparray[i].append(OpStructureTensorEigenvalues(self))
                        oparray[i][j].inputs["Input"].connect(self.source.outputs["Output"])
                        # Note: If you need to change the inner or outer scale,
                        #  you must make a new feature (with a new feature ID) and
                        #  leave this feature here to preserve backwards compatibility
                        oparray[i][j].inputs["innerScale"].setValue(self.newScales[j])
                        oparray[i][j].inputs["outerScale"].setValue(self.newScales[j]*0.5)
                        featureNameArray[i].append("Structure Tensor Eigenvalues (s=" + str(self.scales[j]) + ")")

                elif featureId == 'HessianOfGaussianEigenvalues':
                    for j in range(dimCol):
                        oparray[i].append(OpHessianOfGaussianEigenvalues(self))
                        oparray[i][j].inputs["Input"].connect(self.source.outputs["Output"])
                        oparray[i][j].inputs["scale"].setValue(self.newScales[j])
                        featureNameArray[i].append("Hessian of Gaussian Eigenvalues (s=" + str(self.scales[j]) + ")")

                elif featureId == 'GaussianGradientMagnitude':
                    for j in range(dimCol):
                        oparray[i].append(OpGaussianGradientMagnitude(self))
                        oparray[i][j].inputs["Input"].connect(self.source.outputs["Output"])
                        oparray[i][j].inputs["sigma"].setValue(self.newScales[j])
                        featureNameArray[i].append("Gaussian Gradient Magnitude (s=" + str(self.scales[j]) + ")")

                elif featureId == 'DifferenceOfGaussians':
                    for j in range(dimCol):
                        oparray[i].append(OpDifferenceOfGaussians(self))
                        oparray[i][j].inputs["Input"].connect(self.source.outputs["Output"])
                        # Note: If you need to change sigma0 or sigma1, you must make a new
                        #  feature (with a new feature ID) and leave this feature here
                        #  to preserve backwards compatibility
                        oparray[i][j].inputs["sigma0"].setValue(self.newScales[j])
                        oparray[i][j].inputs["sigma1"].setValue(self.newScales[j]*0.66)
                        featureNameArray[i].append("Difference of Gaussians (s=" + str(self.scales[j]) + ")")

            #disconnecting all Operators
            for islot in self.multi.inputs.values():
                islot.disconnect()

            channelCount = 0
            featureCount = 0
            self.Features.resize( 0 )
            self.featureOutputChannels = []
            #connect individual operators
            for i in range(dimRow):
                for j in range(dimCol):
                    if self.matrix[i,j]:
                        # Feature names are provided via metadata
                        oparray[i][j].outputs["Output"].meta.description = featureNameArray[i][j]
                        self.multi.inputs["Input%02d" %(i*dimCol+j)].connect(oparray[i][j].outputs["Output"])
                        logger.debug("connected  Input%02d of self.multi" %(i*dimCol+j))

                        # Prepare the individual features
                        featureCount += 1
                        self.Features.resize( featureCount )

                        featureMeta = oparray[i][j].outputs["Output"].meta
                        featureChannels = featureMeta.shape[ featureMeta.axistags.index('c') ]
                        self.Features[featureCount-1].meta.assignFrom( featureMeta )
                        self.featureOutputChannels.append( (channelCount, channelCount + featureChannels) )
                        channelCount += featureChannels
            
            #additional connection with FakeOperator
            if (self.matrix==0).all():
                fakeOp = OpGaussianSmoothing(parent=self)
                fakeOp.inputs["Input"].connect(self.source.outputs["Output"])
                fakeOp.inputs["sigma"].setValue(10)
                self.multi.inputs["Input%02d" %(i*dimCol+j+1)].connect(fakeOp.outputs["Output"])
                self.multi.inputs["Input%02d" %(i*dimCol+j+1)].disconnect()
                stackerShape = list(self.Input.meta.shape)
                stackerShape[ self.Input.meta.axistags.index('c') ] = 0
                self.stacker.Output.meta.shape = tuple(stackerShape)
                self.stacker.Output.meta.axistags = self.Input.meta.axistags
            else:
                self.stacker.inputs["AxisFlag"].setValue('c')
                self.stacker.inputs["AxisIndex"].setValue(self.source.outputs["Output"].meta.axistags.index('c'))
                self.stacker.inputs["Images"].connect(self.multi.outputs["Outputs"])
    
                self.maxSigma = 0
                #determine maximum sigma
                for i in range(dimRow):
                    for j in range(dimCol):
                        val=self.matrix[i,j]
                        if val:
                            self.maxSigma = max(self.scales[j],self.maxSigma)
    
                self.featureOps = oparray

            # Output meta is a modified copy of the input meta
            self.Output.meta.assignFrom(self.Input.meta)
            self.Output.meta.dtype = numpy.float32
            self.Output.meta.axistags = self.stacker.Output.meta.axistags
            self.Output.meta.shape = self.stacker.Output.meta.shape

    def propagateDirty(self, inputSlot, subindex, roi):
        if inputSlot == self.Input:
            channelAxis = self.Input.meta.axistags.index('c')
            numChannels = self.Input.meta.shape[channelAxis]
            dirtyChannels = roi.stop[channelAxis] - roi.start[channelAxis]
            
            # If all the input channels were dirty, the dirty output region is a contiguous block
            if dirtyChannels == numChannels:
                dirtyKey = roiToSlice(roi.start, roi.stop)
                dirtyKey[channelAxis] = slice(None)
                dirtyRoi = sliceToRoi(dirtyKey, self.Output.meta.shape)
                self.Output.setDirty(dirtyRoi[0], dirtyRoi[1])
            else:
                # Only some input channels were dirty, 
                #  so we must mark each dirty output region separately.
                numFeatures = self.Output.meta.shape[channelAxis] / numChannels
                for featureIndex in range(numFeatures):
                    startChannel = numChannels*featureIndex + roi.start[channelAxis]
                    stopChannel = startChannel + roi.stop[channelAxis]
                    dirtyRoi = copy.copy(roi)
                    dirtyRoi.start[channelAxis] = startChannel
                    dirtyRoi.stop[channelAxis] = stopChannel
                    self.Output.setDirty(dirtyRoi)

        elif (inputSlot == self.Matrix
              or inputSlot == self.Scales 
              or inputSlot == self.FeatureIds):
            self.Output.setDirty(slice(None))
        else:
            assert False, "Unknown dirty input slot."
            

    def execute(self, slot, subindex, rroi, result):
        assert slot == self.Features or slot == self.Output
        if slot == self.Features:
            key = roiToSlice(rroi.start, rroi.stop)
            index = subindex[0]
            subslot = self.Features[index]
            key = list(key)
            channelIndex = self.Input.meta.axistags.index('c')
            
            # Translate channel slice to the correct location for the output slot.
            key[channelIndex] = slice(self.featureOutputChannels[index][0] + key[channelIndex].start,
                                      self.featureOutputChannels[index][0] + key[channelIndex].stop)
            rroi = SubRegion(subslot, pslice=key)
    
            # Get output slot region for this channel
            return self.execute(self.Output, (), rroi, result)
        elif slot == self.outputs["Output"]:
            key = rroi.toSlice()
            cnt = 0
            written = 0
            assert (rroi.stop<=self.outputs["Output"].meta.shape).all()
            flag = 'c'
            channelAxis=self.inputs["Input"].meta.axistags.index('c')
            axisindex = channelAxis
            oldkey = list(key)
            oldkey.pop(axisindex)


            inShape  = self.inputs["Input"].meta.shape

            shape = self.outputs["Output"].meta.shape

            axistags = self.inputs["Input"].meta.axistags

            result = result.view(vigra.VigraArray)
            result.axistags = copy.copy(axistags)


            hasTimeAxis = self.inputs["Input"].meta.axistags.axisTypeCount(vigra.AxisType.Time)
            timeAxis=self.inputs["Input"].meta.axistags.index('t')

            subkey = popFlagsFromTheKey(key,axistags,'c')
            subshape=popFlagsFromTheKey(shape,axistags,'c')
            at2 = copy.copy(axistags)
            at2.dropChannelAxis()
            subshape=popFlagsFromTheKey(subshape,at2,'t')
            subkey = popFlagsFromTheKey(subkey,at2,'t')

            oldstart, oldstop = roi.sliceToRoi(key, shape)

            start, stop = roi.sliceToRoi(subkey,subkey)
            maxSigma = max(0.7,self.maxSigma)

            # The region of the smoothed image we need to give to the feature filter (in terms of INPUT coordinates)
            vigOpSourceStart, vigOpSourceStop = roi.extendSlice(start, stop, subshape, 0.7, window = 2)
            
            # The region of the input that we need to give to the smoothing operator (in terms of INPUT coordinates)
            newStart, newStop = roi.extendSlice(vigOpSourceStart, vigOpSourceStop, subshape, maxSigma, window = 3.5)

            # Translate coordinates (now in terms of smoothed image coordinates)
            vigOpSourceStart = roi.TinyVector(vigOpSourceStart - newStart)
            vigOpSourceStop = roi.TinyVector(vigOpSourceStop - newStart)

            readKey = roi.roiToSlice(newStart, newStop)


            writeNewStart = start - newStart
            writeNewStop = writeNewStart +  stop - start


            treadKey=list(readKey)

            if hasTimeAxis:
                if timeAxis < channelAxis:
                    treadKey.insert(timeAxis, key[timeAxis])
                else:
                    treadKey.insert(timeAxis-1, key[timeAxis])
            if  self.inputs["Input"].meta.axistags.axisTypeCount(vigra.AxisType.Channels) == 0:
                treadKey =  popFlagsFromTheKey(treadKey,axistags,'c')
            else:
                treadKey.insert(channelAxis, slice(None,None,None))

            treadKey=tuple(treadKey)

            req = self.inputs["Input"][treadKey].allocate()

            sourceArray = req.wait()
            req.result = None
            req.destination = None
            if sourceArray.dtype != numpy.float32:
                sourceArrayF = sourceArray.astype(numpy.float32)
                sourceArray.resize((1,), refcheck = False)
                del sourceArray
                sourceArray = sourceArrayF
            sourceArrayV = sourceArray.view(vigra.VigraArray)
            sourceArrayV.axistags =  copy.copy(axistags)





            dimCol = len(self.scales)
            dimRow = self.matrix.shape[0]


            sourceArraysForSigmas = [None]*dimCol

            #connect individual operators
            for j in range(dimCol):
                hasScale = False
                for i in range(dimRow):
                    if self.matrix[i,j]:
                        hasScale = True
                if not hasScale:
                    continue
                destSigma = 1.0
                if self.scales[j] > destSigma:
                    tempSigma = math.sqrt(self.scales[j]**2 - destSigma**2)
                else:
                    destSigma = 0.0
                    tempSigma = self.scales[j]
                vigOpSourceShape = list(vigOpSourceStop - vigOpSourceStart)
                if hasTimeAxis:

                    if timeAxis < channelAxis:
                        vigOpSourceShape.insert(timeAxis, ( oldstop - oldstart)[timeAxis])
                    else:
                        vigOpSourceShape.insert(timeAxis-1, ( oldstop - oldstart)[timeAxis])
                    vigOpSourceShape.insert(channelAxis, inShape[channelAxis])

                    sourceArraysForSigmas[j] = numpy.ndarray(tuple(vigOpSourceShape),numpy.float32)
                    for i,vsa in enumerate(sourceArrayV.timeIter()):
                        droi = (tuple(vigOpSourceStart._asint()), tuple(vigOpSourceStop._asint()))
                        tmp_key = getAllExceptAxis(len(sourceArraysForSigmas[j].shape),timeAxis, i)
                        sourceArraysForSigmas[j][tmp_key] = vigra.filters.gaussianSmoothing(vsa,tempSigma, roi = droi, window_size = 3.5 )
                else:
                    droi = (tuple(vigOpSourceStart._asint()), tuple(vigOpSourceStop._asint()))
                    #print droi, sourceArray.shape, tempSigma,self.scales[j]
                    sourceArraysForSigmas[j] = vigra.filters.gaussianSmoothing(sourceArrayV, sigma = tempSigma, roi = droi, window_size = 3.5)
                    #sourceArrayForSigma = sourceArrayForSigma.view(numpy.ndarray)

            del sourceArrayV
            try:
                sourceArray.resize((1,), refcheck = False)
            except ValueError:
                # Sometimes this fails, but that's okay.
                logger.debug("Failed to free array memory.")                
            del sourceArray

            
            closures = []

            #connect individual operators
            for i in range(dimRow):
                for j in range(dimCol):
                    val=self.matrix[i,j]
                    if val:
                        vop= self.featureOps[i][j]
                        oslot = vop.outputs["Output"]
                        req = None
                        inTagKeys = [ax.key for ax in oslot.meta.axistags]
                        if flag in inTagKeys:
                            slices = oslot.meta.shape[axisindex]
                            if cnt + slices >= rroi.start[axisindex] and rroi.start[axisindex]-cnt<slices and rroi.start[axisindex]+written<rroi.stop[axisindex]:
                                begin = 0
                                if cnt < rroi.start[axisindex]:
                                    begin = rroi.start[axisindex] - cnt
                                end = slices
                                if cnt + end > rroi.stop[axisindex]:
                                    end -= cnt + end - rroi.stop[axisindex]
                                key_ = copy.copy(oldkey)
                                key_.insert(axisindex, slice(begin, end, None))
                                reskey = [slice(None, None, None) for x in range(len(result.shape))]
                                reskey[axisindex] = slice(written, written+end-begin, None)

                                destArea = result[tuple(reskey)]
                                roi_ = SubRegion(self.Input, pslice=key_)                                
                                closure = partial(oslot.operator.execute, oslot, (), roi_, destArea, sourceArray = sourceArraysForSigmas[j])
                                closures.append(closure)

                                written += end - begin
                            cnt += slices
                        else:
                            if cnt>=rroi.start[axisindex] and rroi.start[axisindex] + written < rroi.stop[axisindex]:
                                reskey = copy.copy(oldkey)
                                reskey.insert(axisindex, written)
                                #print "key: ", key, "reskey: ", reskey, "oldkey: ", oldkey
                                #print "result: ", result.shape, "inslot:", inSlot.shape

                                destArea = result[tuple(reskey)]
                                logger.debug(oldkey, destArea.shape, sourceArraysForSigmas[j].shape)
                                oldroi = SubRegion(self.Input, pslice=oldkey)
                                closure = partial(oslot.operator.execute, oslot, (), oldroi, destArea, sourceArray = sourceArraysForSigmas[j])
                                closures.append(closure)

                                written += 1
                            cnt += 1
            pool = Pool()
            for c in closures:
                r = pool.request(c)
            pool.wait()
            pool.clean()

            for i in range(len(sourceArraysForSigmas)):
                if sourceArraysForSigmas[i] is not None:
                    try:
                        sourceArraysForSigmas[i].resize((1,))
                    except:
                        sourceArraysForSigmas[i] = None


def getAllExceptAxis(ndim,index,slicer):
    res= [slice(None, None, None)] * ndim
    res[index] = slicer
    return tuple(res)

class OpBaseVigraFilter(OpArrayPiper):
    inputSlots = [InputSlot("Input"), InputSlot("sigma", stype = "float")]
    outputSlots = [OutputSlot("Output")]

    name = "OpBaseVigraFilter"
    category = "Vigra filter"

    vigraFilter = None
    outputDtype = numpy.float32
    inputDtype = numpy.float32
    supportsOut = True
    window_size=2
    supportsRoi = False
    supportsWindow = False

    def execute(self, slot, subindex, rroi, result, sourceArray=None):
        assert len(subindex) == self.Output.level == 0
        key = roiToSlice(rroi.start, rroi.stop)

        kwparams = {}
        for islot in self.inputs.values():
            if islot.name != "Input":
                kwparams[islot.name] = islot.value

        if self.inputs.has_key("sigma"):
            sigma = self.inputs["sigma"].value
        elif self.inputs.has_key("scale"):
            sigma = self.inputs["scale"].value
        elif self.inputs.has_key("sigma0"):
            sigma = self.inputs["sigma0"].value
        elif self.inputs.has_key("innerScale"):
            sigma = self.inputs["innerScale"].value

        windowSize = 4.0
        if self.supportsWindow:
            kwparams['window_size']=self.window_size
            windowSize = self.window_size

        largestSigma = sigma #ensure enough context for the vigra operators

        shape = self.outputs["Output"].meta.shape

        axistags = self.inputs["Input"].meta.axistags
        hasChannelAxis = self.inputs["Input"].meta.axistags.axisTypeCount(vigra.AxisType.Channels)
        channelAxis=self.inputs["Input"].meta.axistags.index('c')
        hasTimeAxis = self.inputs["Input"].meta.axistags.axisTypeCount(vigra.AxisType.Time)
        timeAxis=self.inputs["Input"].meta.axistags.index('t')

        subkey = popFlagsFromTheKey(key,axistags,'c')
        subshape=popFlagsFromTheKey(shape,axistags,'c')
        at2 = copy.copy(axistags)
        at2.dropChannelAxis()
        subshape=popFlagsFromTheKey(subshape,at2,'t')
        subkey = popFlagsFromTheKey(subkey,at2,'t')

        oldstart, oldstop = roi.sliceToRoi(key, shape)

        start, stop = roi.sliceToRoi(subkey,subkey)
        newStart, newStop = roi.extendSlice(start, stop, subshape, largestSigma, window = windowSize)
        readKey = roi.roiToSlice(newStart, newStop)

        writeNewStart = start - newStart
        writeNewStop = writeNewStart +  stop - start

        if (writeNewStart == 0).all() and (newStop == writeNewStop).all():
            fullResult = True
        else:
            fullResult = False

        writeKey = roi.roiToSlice(writeNewStart, writeNewStop)
        writeKey = list(writeKey)
        if timeAxis < channelAxis:
            writeKey.insert(channelAxis-1, slice(None,None,None))
        else:
            writeKey.insert(channelAxis, slice(None,None,None))
        writeKey = tuple(writeKey)

        channelsPerChannel = self.resultingChannels()

        if self.supportsRoi is False and largestSigma > 5:
            logger.warn("WARNING: operator", self.name, "does not support roi !!")

        i2 = 0
        for i in range(int(numpy.floor(1.0 * oldstart[channelAxis]/channelsPerChannel)),int(numpy.ceil(1.0 * oldstop[channelAxis]/channelsPerChannel))):
            treadKey=list(readKey)

            if hasTimeAxis:
                if channelAxis > timeAxis:
                    treadKey.insert(timeAxis, key[timeAxis])
                else:
                    treadKey.insert(timeAxis-1, key[timeAxis])
            treadKey.insert(channelAxis, slice(i,i+1,None))
            treadKey=tuple(treadKey)

            if sourceArray is None:
                req = self.inputs["Input"][treadKey].allocate()
                t = req.wait()
            else:
                t = sourceArray[getAllExceptAxis(len(treadKey),channelAxis,slice(i,i+1,None) )]

            t = numpy.require(t, dtype=self.inputDtype)
            t = t.view(vigra.VigraArray)
            t.axistags = copy.copy(axistags)
            t = t.insertChannelAxis()

            sourceBegin = 0

            if oldstart[channelAxis] > i * channelsPerChannel:
                sourceBegin = oldstart[channelAxis] - i * channelsPerChannel
            sourceEnd = channelsPerChannel
            if oldstop[channelAxis] < (i+1) * channelsPerChannel:
                sourceEnd = channelsPerChannel - ((i+1) * channelsPerChannel - oldstop[channelAxis])
            destBegin = i2
            destEnd = i2 + sourceEnd - sourceBegin



            if channelsPerChannel>1:
                tkey=getAllExceptAxis(len(shape),channelAxis,slice(destBegin,destEnd,None))
                resultArea = result[tkey]
            else:
                tkey=getAllExceptAxis(len(shape),channelAxis,slice(i2,i2+1,None))
                resultArea = result[tkey]

            i2 += destEnd-destBegin

            supportsOut = self.supportsOut
            if (destEnd-destBegin != channelsPerChannel):
                supportsOut = False

            supportsOut= False #disable for now due to vigra crashes!
            for step,image in enumerate(t.timeIter()):
                nChannelAxis = channelAxis - 1

                if timeAxis > channelAxis or not hasTimeAxis:
                    nChannelAxis = channelAxis
                twriteKey=getAllExceptAxis(image.ndim, nChannelAxis, slice(sourceBegin,sourceEnd,None))

                if hasTimeAxis > 0:
                    tresKey  = getAllExceptAxis(resultArea.ndim, timeAxis, step)
                else:
                    tresKey  = slice(None, None,None)

                #print tresKey, twriteKey, resultArea.shape, temp.shape
                vres = resultArea[tresKey]
                if supportsOut:
                    if self.supportsRoi:
                        vroi = (tuple(writeNewStart._asint()), tuple(writeNewStop._asint()))
                        try:
                            vres = vres.view(vigra.VigraArray)
                            vres.axistags = copy.copy(image.axistags)
                            print "FAST LANE", self.name, vres.shape, image[twriteKey].shape, vroi
                            temp = self.vigraFilter(image[twriteKey], roi = vroi,out=vres, **kwparams)
                        except:
                            print self.name, image.shape, vroi, kwparams
                    else:
                        try:
                            temp = self.vigraFilter(image, **kwparams)
                        except:
                            print self.name, image.shape, vroi, kwparams
                        temp=temp[writeKey]
                else:
                    if self.supportsRoi:
                        vroi = (tuple(writeNewStart._asint()), tuple(writeNewStop._asint()))
                        try:
                            temp = self.vigraFilter(image, roi = vroi, **kwparams)
                        except Exception, e:
                            print "EXCEPT 2.1", self.name, image.shape, vroi, kwparams
                            traceback.print_exc(e)
                            sys.exit(1)
                    else:
                        try:
                            temp = self.vigraFilter(image, **kwparams)
                        except Exception, e:
                            print "EXCEPT 2.2", self.name, image.shape, kwparams
                            traceback.print_exc(e)
                            sys.exit(1)
                        temp=temp[writeKey]


                    try:
                        vres[:] = temp[twriteKey]
                    except:
                        print "EXCEPT3", vres.shape, temp.shape, twriteKey
                        print "EXCEPT3", resultArea.shape,  tresKey, twriteKey
                        print "EXCEPT3", step, t.shape, timeAxis
                        raise
                
                #print "(in.min=",image.min(),",in.max=",image.max(),") (vres.min=",vres.min(),",vres.max=",vres.max(),")"


    def setupOutputs(self):
        
        # Output meta starts with a copy of the input meta, which is then modified
        self.Output.meta.assignFrom(self.Input.meta)
        
        numChannels  = 1
        inputSlot = self.inputs["Input"]
        if inputSlot.meta.axistags.axisTypeCount(vigra.AxisType.Channels) > 0:
            channelIndex = self.inputs["Input"].meta.axistags.channelIndex
            numChannels = self.inputs["Input"].meta.shape[channelIndex]
            inShapeWithoutChannels = popFlagsFromTheKey( self.inputs["Input"].meta.shape,self.inputs["Input"].meta.axistags,'c')
        else:
            inShapeWithoutChannels = inputSlot.meta.shape
            channelIndex = len(inputSlot.meta.shape)

        self.outputs["Output"].meta.dtype = self.outputDtype
        p = self.inputs["Input"].partner
        at = copy.copy(inputSlot.meta.axistags)

        if at.axisTypeCount(vigra.AxisType.Channels) == 0:
            at.insertChannelAxis()

        self.outputs["Output"].meta.axistags = at

        channelsPerChannel = self.resultingChannels()
        inShapeWithoutChannels = list(inShapeWithoutChannels)
        inShapeWithoutChannels.insert(channelIndex,numChannels * channelsPerChannel)
        self.outputs["Output"].meta.shape = tuple(inShapeWithoutChannels)

        if self.outputs["Output"].meta.axistags.axisTypeCount(vigra.AxisType.Channels) == 0:
            self.outputs["Output"].meta.axistags.insertChannelAxis()

        # The output data range is not necessarily the same as the input data range.
        if 'drange' in self.Output.meta:
            del self.Output.meta['drange']

    def resultingChannels(self):
        raise RuntimeError('resultingChannels() not implemented')


#difference of Gaussians
def differenceOfGausssians(image,sigma0, sigma1,window_size, roi, out = None):
    """ difference of gaussian function"""
    return (vigra.filters.gaussianSmoothing(image,sigma0,window_size=window_size,roi = roi)-vigra.filters.gaussianSmoothing(image,sigma1,window_size=window_size,roi = roi))


def firstHessianOfGaussianEigenvalues(image, **kwargs):
    return vigra.filters.hessianOfGaussianEigenvalues(image, **kwargs)[...,0:1]

def coherenceOrientationOfStructureTensor(image,sigma0, sigma1, window_size, out = None):
    """
    coherence Orientation of Structure tensor function:
    input:  M*N*1ch VigraArray
            sigma corresponding to the inner scale of the tensor
            scale corresponding to the outher scale of the tensor

    output: M*N*2 VigraArray, the firest channel correspond to coherence
                              the second channel correspond to orientation
    """

    #FIXME: make more general

    #assert image.spatialDimensions==2, "Only implemented for 2 dimensional images"
    assert len(image.shape)==2 or (len(image.shape)==3 and image.shape[2] == 1), "Only implemented for 2 dimensional images"

    st=vigra.filters.structureTensor(image, sigma0, sigma1, window_size = window_size)
    i11=st[:,:,0]
    i12=st[:,:,1]
    i22=st[:,:,2]

    if out is not None:
        assert out.shape[0] == image.shape[0] and out.shape[1] == image.shape[1] and out.shape[2] == 2
        res = out
    else:
        res=numpy.ndarray((image.shape[0],image.shape[1],2))

    res[:,:,0]=numpy.sqrt( (i22-i11)**2+4*(i12**2))/(i11-i22)
    res[:,:,1]=numpy.arctan(2*i12/(i22-i11))/numpy.pi +0.5


    return res



class OpDifferenceOfGaussians(OpBaseVigraFilter):
    name = "DifferenceOfGaussians"
    vigraFilter = staticmethod(differenceOfGausssians)
    outputDtype = numpy.float32
    supportsOut = False
    supportsWindow = True
    supportsRoi = True
    inputSlots = [InputSlot("Input"), InputSlot("sigma0", stype = "float"), InputSlot("sigma1", stype = "float")]

    def resultingChannels(self):
        return 1

class OpGaussianSmoothing(OpBaseVigraFilter):
    name = "GaussianSmoothing"
    vigraFilter = staticmethod(vigra.filters.gaussianSmoothing)
    outputDtype = numpy.float32
    supportsRoi = True
    supportsWindow = True
    supportsOut = True

    def resultingChannels(self):
        return 1

class OpHessianOfGaussianEigenvalues(OpBaseVigraFilter):
    name = "HessianOfGaussianEigenvalues"
    vigraFilter = staticmethod(vigra.filters.hessianOfGaussianEigenvalues)
    outputDtype = numpy.float32
    supportsRoi = True
    supportsWindow = True
    supportsOut = True
    inputSlots = [InputSlot("Input"), InputSlot("scale", stype = "float")]

    def resultingChannels(self):
        temp = self.inputs["Input"].meta.axistags.axisTypeCount(vigra.AxisType.Space)
        return temp


class OpStructureTensorEigenvalues(OpBaseVigraFilter):
    name = "StructureTensorEigenvalues"
    vigraFilter = staticmethod(vigra.filters.structureTensorEigenvalues)
    outputDtype = numpy.float32
    supportsRoi = True
    supportsWindow = True
    supportsOut = True
    inputSlots = [InputSlot("Input"), InputSlot("innerScale", stype = "float"),InputSlot("outerScale", stype = "float")]

    def resultingChannels(self):
        temp = self.inputs["Input"].meta.axistags.axisTypeCount(vigra.AxisType.Space)
        return temp



class OpHessianOfGaussianEigenvaluesFirst(OpBaseVigraFilter):
    name = "First Eigenvalue of Hessian Matrix"
    vigraFilter = staticmethod(firstHessianOfGaussianEigenvalues)
    outputDtype = numpy.float32
    supportsOut = False
    supportsWindow = True
    supportsRoi = True

    inputSlots = [InputSlot("Input"), InputSlot("scale", stype = "float")]

    def resultingChannels(self):
        return 1



class OpHessianOfGaussian(OpBaseVigraFilter):
    name = "HessianOfGaussian"
    vigraFilter = staticmethod(vigra.filters.hessianOfGaussian)
    outputDtype = numpy.float32
    supportsWindow = True
    supportsRoi = True
    supportsOut = True

    def resultingChannels(self):
        temp = self.inputs["Input"].meta.axistags.axisTypeCount(vigra.AxisType.Space)*(self.inputs["Input"].meta.axistags.axisTypeCount(vigra.AxisType.Space) + 1) / 2
        return temp

class OpGaussianGradientMagnitude(OpBaseVigraFilter):
    name = "GaussianGradientMagnitude"
    vigraFilter = staticmethod(vigra.filters.gaussianGradientMagnitude)
    outputDtype = numpy.float32
    supportsRoi = True
    supportsWindow = True
    supportsOut = True

    def resultingChannels(self):
        return 1

class OpLaplacianOfGaussian(OpBaseVigraFilter):
    name = "LaplacianOfGaussian"
    vigraFilter = staticmethod(vigra.filters.laplacianOfGaussian)
    outputDtype = numpy.float32
    supportsOut = True
    supportsRoi = True
    supportsWindow = True
    inputSlots = [InputSlot("Input"), InputSlot("scale", stype = "float")]


    def resultingChannels(self):
        return 1

class OpImageReader(Operator):
    name = "Image Reader"
    category = "Input"

    inputSlots = [InputSlot("Filename", stype = "filestring")]
    outputSlots = [OutputSlot("Image")]

    def setupOutputs(self):
        filename = self.inputs["Filename"].value

        if filename is not None:
            info = vigra.impex.ImageInfo(filename)

            oslot = self.outputs["Image"]
            oslot.meta.shape = info.getShape()
            oslot.meta.dtype = info.getDtype()
            oslot.meta.axistags = info.getAxisTags()
            
            numImages = vigra.impex.numberImages(filename)
            if numImages > 1:
                taggedShape = oslot.meta.getTaggedShape()
                assert 'z' not in taggedShape.keys(), "Didn't expect to find a z-axis in this image."
                # Convert from OrderedDict to list
                taggedShape = [(key, dim) for key, dim in taggedShape.items()]

                # Insert z-shape
                taggedShape.insert(-1, ('z', numImages))

                # Insert z-tag
                tags = oslot.meta.axistags
                tags.insert(-1, vigra.defaultAxistags('z')[0])

                oslot.meta.shape = tuple(dim for (key, dim) in taggedShape)
                oslot.meta.axistags = tags
        else:
            oslot = self.outputs["Image"]
            oslot.meta.shape = None
            oslot.meta.dtype = None
            oslot.meta.axistags = None

    def execute(self, slot, subindex, rroi, result):
        key = roiToSlice(rroi.start, rroi.stop)
        filename = self.inputs["Filename"].value
        index = 0
        taggedShape = self.Image.meta.getTaggedShape()
        if 'z' in taggedShape.keys():
            zIndex = taggedShape.keys().index('z')
            tempShape = list(self.Image.meta.shape)
            tempShape[zIndex] = rroi.stop[zIndex] - rroi.start[zIndex]
            temp = numpy.ndarray( tempShape, dtype=self.Image.meta.dtype )
            
            for i,z in enumerate(range(rroi.start[zIndex], rroi.stop[zIndex])):
                tempKey = list(key)
                tempKey[zIndex] = i
                temp[tempKey] = vigra.impex.readImage(filename, index=z)

            key = list(key)
            key[zIndex] = slice(0, rroi.stop[zIndex] - rroi.start[zIndex] )
            key = tuple(key)
        else:
            temp = vigra.impex.readImage(filename)

        return temp[key]

    def propagateDirty(self, slot, subindex, roi):
        if slot == self.Filename:
            self.Image.setDirty(slice(None))
        else:
            assert False, "Unknown dirty input slot."

class OpH5WriterBigDataset(Operator):
    name = "H5 File Writer BigDataset"
    category = "Output"

    inputSlots = [InputSlot("hdf5File"), # Must be an already-open hdf5File (or group) for writing to
                  InputSlot("hdf5Path", stype = "string"),
                  InputSlot("Image")]

    outputSlots = [OutputSlot("WriteImage")]

    def __init__(self, *args, **kwargs):
        super(OpH5WriterBigDataset, self).__init__(*args, **kwargs)
        self.progressSignal = OrderedSignal()

    def setupOutputs(self):
        self.outputs["WriteImage"].meta.shape = (1,)
        self.outputs["WriteImage"].meta.dtype = object

        self.f = self.inputs["hdf5File"].value
        hdf5Path = self.inputs["hdf5Path"].value
        
        # On windows, there may be backslashes.
        hdf5Path = hdf5Path.replace('\\', '/')

        hdf5GroupName, datasetName = os.path.split(hdf5Path)
        if hdf5GroupName == "":
            g = self.f
        else:
            if hdf5GroupName in self.f:
                g = self.f[hdf5GroupName]
            else:
                g = self.f.create_group(hdf5GroupName)

        dataShape=self.Image.meta.shape
        axistags = self.Image.meta.axistags
        dtype = self.Image.meta.dtype
        if type(dtype) is numpy.dtype:
            # Make sure we're dealing with a type (e.g. numpy.float64),
            #  not a numpy.dtype
            dtype = dtype.type

        numChannels = dataShape[ axistags.index('c') ]

        # Set up our chunk shape: Aim for a cube that's roughly 300k in size
        dtypeBytes = dtype().nbytes
        cubeDim = math.pow( 300000 / (numChannels * dtypeBytes), (1/3.0) )
        cubeDim = int(cubeDim)

        chunkDims = {}
        chunkDims['t'] = 1
        chunkDims['x'] = cubeDim
        chunkDims['y'] = cubeDim
        chunkDims['z'] = cubeDim
        chunkDims['c'] = numChannels
        
        # h5py guide to chunking says chunks of 300k or less "work best"
        assert chunkDims['x'] * chunkDims['y'] * chunkDims['z'] * numChannels * dtypeBytes  <= 300000

        chunkShape = ()
        for i in range( len(dataShape) ):
            axisKey = self.Image.meta.axistags[i].key
            # Chunk shape can't be larger than the data shape
            chunkShape += ( min( chunkDims[axisKey], dataShape[i] ), )

        self.chunkShape = chunkShape
        if datasetName in g.keys():
            del g[datasetName]
        self.d=g.create_dataset(datasetName,
                                shape=dataShape,
                                dtype=dtype,
                                chunks=self.chunkShape,
                                compression='gzip',
                                compression_opts=4)

        if 'drange' in self.Image.meta:
            self.d.attrs['drange'] = self.Image.meta.drange

    def execute(self, slot, subindex, rroi, result):
        key = roiToSlice(rroi.start, rroi.stop)
        self.progressSignal(0)
        
        slicings=self.computeRequestSlicings()
        numSlicings = len(slicings)
        imSlot = self.inputs["Image"]

        # Throttle: Only allow 10 outstanding requests at a time.
        # Otherwise, the whole set of requests can be outstanding and use up ridiculous amounts of memory.        
        activeRequests = deque()
        activeSlicings = deque()
        # Start by activating 10 requests 
        for i in range( min(10, len(slicings)) ):
            s = slicings.pop()
            activeSlicings.append(s)
            logger.debug( "Creating request for slicing {}".format(s) )
            activeRequests.append( self.inputs["Image"][s] )
        
        counter = 0

        while len(activeRequests) > 0:
            # Wait for a request to finish
            req = activeRequests.popleft()
            s=activeSlicings.popleft()
            data = req.wait()
            self.d[s]=data

            if len(slicings) > 0:
                # Create a new active request
                s = slicings.pop()
                activeSlicings.append(s)
                activeRequests.append( self.inputs["Image"][s] )
            
            # Since requests finish in an arbitrary order (but we always block for them in the same order),
            # this progress feedback will not be smooth.  It's the best we can do for now.
            self.progressSignal( 100*counter/numSlicings )
            logger.debug( "request {} out of {} executed".format( counter, numSlicings ) )
            counter += 1

        # Save the axistags as a dataset attribute
        self.d.attrs['axistags'] = self.Image.meta.axistags.toJSON()

        # We're finished.
        result[0] = True

        self.progressSignal(100)

    def computeRequestSlicings(self):
        #TODO: reimplement the request better
        shape=numpy.asarray(self.inputs['Image'].meta.shape)

        chunkShape = numpy.asarray(self.chunkShape)

        # Choose a request shape that is a multiple of the chunk shape
        axistags = self.Image.meta.axistags
        multipliers = { 'x':2, 'y':2, 'z':2, 't':1, 'c':10 }
        multiplier = [multipliers[tag.key] for tag in axistags ]
        shift = chunkShape * numpy.array(multiplier)
        shift=numpy.minimum(shift,shape)
        start=numpy.asarray([0]*len(shape))

        stop=shift
        reqList=[]

        #shape = shape - (numpy.mod(numpy.asarray(shape),
        #                  shift))
        from itertools import product

        for indices in product(*[range(0, stop, step)
                        for stop,step in zip(shape, shift)]):

            start=numpy.asarray(indices)
            stop=numpy.minimum(start+shift,shape)
            reqList.append(roiToSlice(start,stop))
        return reqList

    def propagateDirty(self, slot, subindex, roi):
        # The output from this operator isn't generally connected to other operators.
        # If someone is using it that way, we'll assume that the user wants to know that 
        #  the input image has become dirty and may need to be written to disk again.
        self.WriteImage.setDirty(slice(None))
