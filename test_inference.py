import torch
from torchvision import datasets, transforms
from torch.utils.data import DataLoader
import numpy as np
from datetime import datetime
from BitNetMCU import FCMNIST, QuantizedModel
from ctypes import CDLL, c_uint32, c_int8, c_uint8, POINTER
import argparse
import yaml

# Export quantized model from saved checkpoint
# cpldcpu 2024-04-14
# Note: Hyperparameters are used to generated the filename
#---------------------------------------------

def create_run_name(hyperparameters):
    runname = hyperparameters["runtag"] + hyperparameters["scheduler"] + '_lr' + str(hyperparameters["learning_rate"]) + ('_Aug' if hyperparameters["augmentation"] else '') + '_BitMnist_' + hyperparameters["WScale"] + "_" +hyperparameters["QuantType"] + "_" + hyperparameters["NormType"] + "_width" + str(hyperparameters["network_width1"]) + "_" + str(hyperparameters["network_width2"]) + "_" + str(hyperparameters["network_width3"])  + "_bs" + str(hyperparameters["batch_size"]) + "_epochs" + str(hyperparameters["num_epochs"])
    hyperparameters["runname"] = runname
    return runname

def export_test_data_to_c(test_loader, filename, num=8):
    with open(filename, 'w') as f:
        for i, (input_data, labels) in enumerate(test_loader):
            if i >= num:
                break
            # Reshape and convert to numpy
            input_data = input_data.view(input_data.size(0), -1).cpu().numpy()
            labels = labels.cpu().numpy()

            scale = 31.0 / np.maximum(np.abs(input_data).max(axis=-1, keepdims=True), 1e-5)
            scaled_data = np.round(input_data * scale).clip(-31, 31)

            # Convert to C array declarations
            for j, data in enumerate(scaled_data):
                f.write(f'int8_t input_data_{i}[64] = ' + '{' + ', '.join(map(str, data.flatten())) + '};\n')

            f.write(f'uint8_t label_{i} = ' + str(labels[0]) + ';\n')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Training script')
    parser.add_argument('--params', type=str, help='Name of the parameter file', default='trainingparameters.yaml')
    
    args = parser.parse_args()
    
    if args.params:
        paramname = args.params
    else:
        paramname = 'trainingparameters.yaml'

    print(f'Load parameters from file: {paramname}')
    with open(paramname) as f:
        hyperparameters = yaml.safe_load(f)

    # main
    runname= create_run_name(hyperparameters)
    print(runname)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load the MNIST dataset
    transform = transforms.Compose([
        transforms.Resize((8, 8)),  # Resize images to 16x16
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,))
    ])

    train_data = datasets.MNIST(root='data', train=True, transform=transform, download=True)
    test_data = datasets.MNIST(root='data', train=False, transform=transform)
    # Create data loaders
    test_loader = DataLoader(test_data, batch_size=hyperparameters["batch_size"], shuffle=False)

    # Initialize the network and optimizer
    model = FCMNIST(
        network_width1=hyperparameters["network_width1"], 
        network_width2=hyperparameters["network_width2"], 
        network_width3=hyperparameters["network_width3"], 
        QuantType=hyperparameters["QuantType"], 
        NormType=hyperparameters["NormType"],
        WScale=hyperparameters["WScale"]
    ).to(device)

    print('Loading model...')    
    try:
        model.load_state_dict(torch.load(f'modeldata/{runname}.pth'))
    except FileNotFoundError:
        print(f"The file 'modeldata/{runname}.pth' does not exist.")
        exit()

    print('Inference using the original model...')
    correct = 0
    total = 0
    test_loss = []
    with torch.no_grad():
        for images, labels in test_loader:
            images, labels = images.to(device), labels.to(device)        
            outputs = model(images)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
    testaccuracy = correct / total * 100
    print(f'Accuracy/Test of trained model: {testaccuracy} %')

    print('Quantizing model...')
    # Quantize the model
    quantized_model = QuantizedModel(model)
    print(f'Total number of bits: {quantized_model.totalbits()} ({quantized_model.totalbits()/8/1024} kbytes)')

    # Inference using the quantized model
    print ("Verifying inference of quantized model in Python and C")

   # Initialize counter
    counter = 0
    correct_c = 0
    correct_py = 0
    mismatch = 0

    test_loader2 = DataLoader(test_data, batch_size=1, shuffle=True)    

    export_test_data_to_c(test_loader2, 'BitNetMCU_MNIST_test_data.h', num=10)

    lib = CDLL('./Bitnet_inf.dll')

    for input_data, labels in test_loader2:
        # Reshape and convert to numpy
        input_data = input_data.view(input_data.size(0), -1).cpu().numpy()
        labels = labels.cpu().numpy()

        scale = 127.0 / np.maximum(np.abs(input_data).max(axis=-1, keepdims=True), 1e-5)
        scaled_data = np.round(input_data * scale).clip(-128, 127) 

        input_data_ctypes = (c_int8 * len(scaled_data.flatten()))(*scaled_data.astype(np.int8).flatten())

        # Create a pointer to the ctypes array
        input_data_pointer = POINTER(c_int8)(input_data_ctypes)

        lib.Inference.argtypes = [POINTER(c_int8)]
        lib.Inference.restype = c_uint32

        result_c = lib.Inference(input_data_pointer)

    # Inference
        result_py = quantized_model.inference_quantized(input_data)
        predict_py = np.argmax(result_py, axis=1)

        activations = quantized_model.get_activations(input_data)

        # weights = np.array(quantized_model.quantized_model[0]['quantized_weights'])
        # print(weights.shape)
        # print(f'weights: {weights[0]}')
        # print(f'Activations: {activations[0]} len: {len(activations[0][0])}')
        # print(f'Activations: {activations[1]} len: {len(activations[1][0])}')
        # exit()

        if (result_c == labels[0]):
            correct_c += 1

        if (predict_py[0] == labels[0]):
            correct_py += 1

        if (result_c != predict_py[0]):
            print(f'Mismatch between inference engines found. Preduction C: {result_c} Prediction Python: {predict_py[0]} True: {labels[0]}')
            mismatch +=1

        counter += 1

    print("size of test data:", counter)
    print(f'Mispredictions C: {counter - correct_c} Py: {counter - correct_py}')
    print('Overall accuracy C:', correct_c / counter * 100, '%')
    print('Overall accuracy Python:', correct_py / counter * 100, '%')
    
    print(f'Mismatches between engines: {mismatch} ({mismatch/counter*100}%)')