import spacy
nlp = spacy.load("en_core_web_sm")
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
import os
import time
import pickle
import locale
from spacy.lang.en.stop_words import STOP_WORDS
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, confusion_matrix, classification_report, fbeta_score
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.svm import SVC
from sklearn.model_selection import StratifiedKFold, GridSearchCV, train_test_split
import networkx as nx
import torch
from transformers import AutoTokenizer, AutoModel, BioGptModel, AutoModelForSequenceClassification, AdamW, AutoModelForCausalLM, BioGptForCausalLM,GPT2ForSequenceClassification, Trainer, TrainingArguments
from torch.utils.data import Dataset, DataLoader, TensorDataset
import warnings
warnings.filterwarnings("ignore")
import gc
torch.cuda.empty_cache()
gc.collect()
import torch.nn.functional as F
from lime.lime_text import LimeTextExplainer
import sys

def get_components(term_matching):
    """
    Finds the connected components of a graph created from a dataframe containing two columns 'Term1' and 'Term2'.

    Args:
        term_matching (pandas.DataFrame): A dataframe containing two columns 'Term1' and 'Term2'.

    Returns:
        list: A list of connected components, where each component is a set of nodes.
    """
    if term_matching.empty:
        raise ValueError("Input dataframe is empty.")
    
    # Create an undirected graph from the dataframe
    graph = nx.Graph()
    graph.add_edges_from([(first, second) for first, second in zip(term_matching['Term1'], term_matching['Term2'])])

    # Find the connected components
    components = list(nx.connected_components(graph))

    return components


def get_plain_embedding_similarity(mod, model, tokenizer, term_matching, word_pairs):
    """
    Calculates the cosine similarity between the plain embeddings of the word pairs generated by a transformer model.

    Args:
        mod (str): The name of the transformer model.
        model (transformers.PreTrainedModel): The pre-trained transformer model.
        tokenizer (transformers.PreTrainedTokenizer): The pre-trained tokenizer used to tokenize the input words.
        term_matching (pandas.DataFrame): A dataframe containing two columns 'Term1' and 'Term2'.
        word_pairs (list): A list of tuples, where each tuple contains two words that form a pair.

    Returns:
        pandas.DataFrame: The input dataframe with an additional column 'mod_Cosine_Similarity' containing the cosine similarity scores.
    """

    # Generate Model embeddings for each word in the pair
    embeddings = []
    model.to('cpu')   # move model to CPU for faster inference
    for pair in word_pairs:
        inputs = tokenizer(pair, padding=True, truncation=True, return_tensors="pt")
        with torch.no_grad():
            outputs = model(**inputs)
            embeddings.append(outputs.hidden_states[-1].mean(dim=1).squeeze().numpy())  # get plain embedding for each word in the pair and store it

    # Compute the cosine similarity between Model embeddings for each pair
    similarities = [cosine_similarity([word[0],word[1]])[0][1] for word in embeddings]  # calculate cosine similarity score between embeddings of each pair
    term_matching[mod+'_Cosine_Similarity'] = similarities   # add the cosine similarity score to the input dataframe
    return term_matching  # return the updated dataframe

def preprocess(text):
    """
    Preprocesses input text by parsing it using spaCy, extracting named entities, replacing abbreviations,
    lemmatizing tokens, and removing stop words, punctuation, and spaces.

    Args:
        text (str): The input text to preprocess.

    Returns:
        pd.Series: A Pandas series containing the named entities from the original text, preprocessed text, and both.
    """
    # Use spaCy to parse the input text and extract named entities
    doc_raw = nlp(text)
    ner_raw = []
    for word in doc_raw.ents:
        ner_temp = [word.lemma_,word.label_]
        ner_raw.append(ner_temp)

    # Replace the abbreviation 'y/o' with 'year old'
    text = text.replace('y/o',' year old ')

    # Use spaCy to parse the text again and preprocess it
    doc = nlp(text)
    # Join the lemmatized tokens into a string, removing stop words, punctuation, and spaces
    text = " ".join([str(token.lemma_).strip().lower().replace(',','') for token in doc if token.is_stop == False and not token.is_punct and not token.is_space and len(str(token.lemma_).strip().lower()) >0])
    # Extract named entities from the preprocessed text
    ner_preprocess = []
    for word in doc.ents:
        ner_preprocess_temp = [word.lemma_,word.label_]
        ner_preprocess.append(ner_preprocess_temp)

    # Return the named entities from the original text, preprocessed text, and both
    return pd.Series([ner_raw,text,ner_preprocess])

def preprocess_notes(medical_notes, label_dict):
    """
    Preprocess medical notes data and try to capture NER tags.

    Args:
        medical_notes (pandas.DataFrame): The medical notes data to preprocess.
        label_dict (dict): A dictionary mapping category labels to numerical values.

    Returns:
        Tuple[pandas.DataFrame, dict]: A tuple containing the preprocessed medical notes data and the label dictionary.
    """
    print(medical_notes['category'].value_counts())
    medical_notes['category_label'] = medical_notes['category'].apply(lambda x: label_dict[x])
    medical_notes[['ner_notes','notes_preprocess','ner_preprocess_notes']] = medical_notes['notes'].apply(preprocess)
    return medical_notes, label_dict

def Analyze_models(model_dict, word_pairs, medical_notes, term_matching,label_dict):
    """
    Fine-tunes different models on medical notes data, evaluates their performance,
    and checks similarity between pairs of words using the models' embeddings.
    
    Args:
        model_dict (dict): A dictionary containing the names and pretrained models to use.
        word_pairs (list): A list of tuples of word pairs to compare for similarity.
        medical_notes (DataFrame): A pandas DataFrame containing medical notes data.
        term_matching (dict): A dictionary of terms and their embeddings to match for similarity. 
        label_dict (dict) : A dictionary of category and their numerical labels.
        
    Returns:
        Tuple: A tuple containing the medical notes DataFrame, the updated term_matching dictionary,
        and a dictionary of results for each model.
    """
    AllResult = {}
    path_models = './models/'
    if not os.path.exists(path_models):
        os.makedirs(path_models)
    # Load different Models and tokenizers
    for mod, value in model_dict.items():
        tokenizer = AutoTokenizer.from_pretrained(value['Tokenizer'])
        if mod == 'BioGPT':
            model = GPT2ForSequenceClassification.from_pretrained(
                value['Tokenizer'], num_labels=len(list(label_dict.keys())), output_hidden_states=True)
        else:
            model = AutoModelForSequenceClassification.from_pretrained(
                value['Model'], num_labels=len(list(label_dict.keys())), output_hidden_states=True)
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        model.to(device)

        # Get direct embeddings from model and check similarity between pairs in term_matching
        term_matching = get_plain_embedding_similarity(
            mod, model, tokenizer, term_matching, word_pairs)
        print(mod+"_Raw")
        
        # Fine tune models on Raw notes data and get Performance Metrics
        result_scores, class_report, term_matching = fine_tune(
            mod, model, tokenizer, medical_notes, label_dict, 'notes', term_matching, word_pairs)
        AllResult[mod+'_Raw'] = {'Scores': result_scores, 'Report': class_report}

        # Reinitialize Tokenizer and model
        tokenizer = AutoTokenizer.from_pretrained(value['Tokenizer'])
        if mod == 'BioGPT':
            model = GPT2ForSequenceClassification.from_pretrained(
                value['Tokenizer'], num_labels=len(list(label_dict.keys())), output_hidden_states=True)
        else:
            model = AutoModelForSequenceClassification.from_pretrained(
                value['Model'], num_labels=len(list(label_dict.keys())), output_hidden_states=True)
        model.to(device)
        print('-'*100)
        print(mod+"_Preprocess")
        
        # Fine tune models on Preprocessed notes data and get Performance Metrics
        result_scores_preprocess, class_report_preprocess, term_matching = fine_tune(
            mod, model, tokenizer, medical_notes, label_dict, 'notes_preprocess', term_matching, word_pairs)
        AllResult[mod+'_Preprocess'] = {'Scores': result_scores_preprocess, 'Report': class_report_preprocess}
        print('#'*100)
    return medical_notes, term_matching, AllResult

def fine_tune(mod, model, tokenizer, medical_notes, label_dict,type_data,term_matching,word_pairs):
    """
    This function fine-tunes a given transformer model on a medical notes dataset, and returns performance metrics,
    term matching embedding similarity and saves the fine-tuned model, tokenizer and configuration, training and test files.

    Arguments:

        mod (string): Name of the transformer model to be fine-tuned.
        model (transformer model): Pre-trained transformer model to be fine-tuned.
        tokenizer (tokenizer): Pre-trained tokenizer corresponding to the model.
        medical_notes (pandas dataframe): Pandas dataframe containing the medical notes dataset.
        label_dict (dictionary): Dictionary mapping category labels to category names.
        type_data (string): Name of the column containing the text data in the medical_notes dataframe.
        term_matching (dictionary): Dictionary containing terms for term-matching analysis.
        word_pairs (list): List of tuples containing word pairs for term-matching analysis.
    Returns:

        result_scores (dictionary): Dictionary containing various performance metrics.
        class_report (string): String containing a classification report of the model.
        term_matching (dictionary): Dictionary containing term matching embeddings after fine-tuning.
"""
    # Define dataset
    class MyDataset(Dataset):
        def __init__(self, texts, labels, tokenizer):
            self.texts = texts
            self.labels = labels
            self.tokenizer = tokenizer

        def __len__(self):
            return len(self.texts)

        def __getitem__(self, idx):
            text = self.texts[idx]
            label = self.labels[idx]
            inputs = self.tokenizer.encode_plus(text, add_special_tokens=True, return_token_type_ids=False,
                                                padding='max_length', truncation=True, max_length=128)
            return {
                'input_ids': torch.tensor(inputs['input_ids'], dtype=torch.long),
                'attention_mask': torch.tensor(inputs['attention_mask'], dtype=torch.long),
                'labels': torch.tensor(label, dtype=torch.long)
            }

    # Splitting Between Dependent and Independent Variables
    data_X = medical_notes[[type_data]]
    data_Y = medical_notes[['category_label']]

    torch.cuda.empty_cache()
    random_seed = 12

    # Splitting data into test and train using sklearn stratified test_train_split
    X_train, X_test, y_train, y_test = train_test_split(data_X,data_Y,test_size=0.2, random_state=random_seed,stratify=data_Y)
    
    # Preparing Train_csv and Test_csv to store data and results for each models
    label_dict_map = {v:k for k,v in label_dict.items()}
    Train_csv = pd.concat([X_train,y_train],axis=1).reset_index(drop=True)
    Test_csv = pd.concat([X_test,y_test],axis=1).reset_index(drop=True)
    Train_csv['category'] = Train_csv['category_label'].apply(lambda x:label_dict_map[x])
    Test_csv['category'] = Test_csv['category_label'].apply(lambda x:label_dict_map[x])

    train_texts = list(X_train[type_data])
    train_labels = list(y_train['category_label'])

    test_texts = list(X_test[type_data])
    test_labels = list(y_test['category_label'])

    # Define optimizer and training settings
    optimizer = AdamW(model.parameters(), lr=2e-5)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model.to(device)

    # Loading test and train data, varyying batch size and epochs for different models
    if mod == 'BioGPT':
        train_dataset = MyDataset(train_texts, train_labels, tokenizer)
        train_dataloader = DataLoader(train_dataset, batch_size=2, shuffle=True)
        test_dataset = MyDataset(test_texts, test_labels, tokenizer)
        test_dataloader = DataLoader(test_dataset, batch_size=2, shuffle=False)
        model.resize_token_embeddings(len(tokenizer))
        model.lm_head = torch.nn.Linear(in_features=model.config.hidden_size, out_features=len(list(label_dict.keys())), bias=True)
        num_epochs = 10

    else:
        train_dataset = MyDataset(train_texts, train_labels, tokenizer)
        train_dataloader = DataLoader(train_dataset, batch_size=8, shuffle=True)
        test_dataset = MyDataset(test_texts, test_labels, tokenizer)
        test_dataloader = DataLoader(test_dataset, batch_size=8, shuffle=False)
        num_epochs = 5

    # Fine-tune model  
    for epoch in range(num_epochs):
      model.train() 
      for batch in train_dataloader:
          input_ids = batch['input_ids'].to(device)
          attention_mask = batch['attention_mask'].to(device)
          labels = batch['labels'].to(device)
          outputs = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
          loss = outputs.loss
          loss.backward()
          optimizer.step()
          optimizer.zero_grad()


          
    # Evaluate on test set
    model.eval()
    predictions = []
    with torch.no_grad():
        for batch in test_dataloader:
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            outputs = model(input_ids, attention_mask=attention_mask)
            logits = outputs.logits
            batch_predictions = torch.argmax(logits, axis=1).tolist()
            predictions.extend(batch_predictions)

    Test_csv = pd.concat([Test_csv,pd.DataFrame(predictions,columns=['Prediction_label'])],axis=1)
    Test_csv['Prediction'] = Test_csv['Prediction_label'].apply(lambda x:label_dict_map[x])
    
    # Save all model results under given path
    output_path = "./models/"+mod+"_"+type_data+"/"
    if not os.path.exists(output_path):
        os.makedirs(output_path)

    # Save Model, Config and Tokenizer, Traing and Test file
    model.save_pretrained(output_path)
    tokenizer.save_pretrained(output_path)
    model.config.to_json_file(output_path+"config.json")
    Train_csv.to_csv(output_path+'train_data.csv',index=False)
    Test_csv.to_csv(output_path+'test_predictions.csv',index=False)

    # return test_labels,predictions,label_dict
    result_scores,class_report = PerformanceMetrics(test_labels,predictions,label_dict,output_path)

    # Get term matching embedding after fine tuning
    term_matching = get_plain_embedding_similarity(mod+"_fine_tune_"+type_data,model,tokenizer,term_matching,word_pairs)


    return result_scores,class_report,term_matching


def get_score(y_ground, y_pred_ground, average='weighted'):
    """
    Calculates and returns the accuracy, precision, recall, and f1-score of the predicted labels.

    Parameters:
    y_ground (array-like): The ground truth labels.
    y_pred_ground (array-like): The predicted labels.
    average (string, optional): The averaging method for multi-class classification. Default is 'weighted'.

    Returns:
    Pandas DataFrame: A DataFrame containing the calculated scores with column names 'Accuracy', 'Precision', 'Recall', and 'F1-Score'.
    """
    score = [[round(accuracy_score(y_ground,y_pred_ground)*100.0,3),round(precision_score(y_ground,y_pred_ground,average=average)*100.0,3),round(recall_score(y_ground,y_pred_ground,average=average)*100.0,3),round(f1_score(y_ground,y_pred_ground,average=average)*100.0,3)]]
    return pd.DataFrame(score, columns=['Accuracy', 'Precision', 'Recall', 'F1-Score'])

def PerformanceMetrics(y_ground, y_pred_ground, label_dict,output_path):
    """
    Calculates and prints the performance metrics, including accuracy, precision, recall, f1-score, confusion matrix, and classification report.

    Parameters:
    y_ground (array-like): The ground truth labels.
    y_pred_ground (array-like): The predicted labels.
    label_dict (dict): A dictionary containing the label names as keys and their corresponding integer values as values.
    output_path (str): Model Specific Path to save confusion matrix
    Returns:
    tuple: A tuple containing the result_scores and classification_report_df DataFrames.
    """
    # Get the performance metrics scores
    result_scores = get_score(y_ground, y_pred_ground)

    # Print the scores
    print(result_scores.head(10))

    # Get and print the confusion matrix
    get_confusion(y_ground, y_pred_ground, label_dict, output_path, sample_weight=None, normalize=None, xlab="Predicted Label", ylab="Actual Label")

    # Get the classification report
    report = classification_report(y_ground, y_pred_ground, target_names=list(label_dict.keys()), output_dict=True)

    # Create a DataFrame from the classification report
    classification_report_df = pd.DataFrame(report).transpose()

    # Print the classification report
    print(classification_report_df.head(10))

    # Return the DataFrames
    return result_scores, classification_report_df


def get_confusion(y_ground, y_pred_ground, label_dict, output_path,sample_weight=None, normalize=None, xlab="Predicted Label", ylab="Actual Label"):
    """
    Plot and saves the confusion matrix for the given ground truth and predicted labels.

    Args:
    - y_ground: array-like of shape (n_samples,), true labels for samples.
    - y_pred_ground: array-like of shape (n_samples,), predicted labels for samples.
    - label_dict: dict, a dictionary of label names and their corresponding indices in the confusion matrix.
    - sample_weight: array-like of shape (n_samples,), optional, Sample weights.
    - normalize: str or None, optional, 'true', 'pred', 'all', Normalize confusion matrix over the true (rows), predicted (columns) or all population. If None, confusion matrix will not be normalized.
    - xlab: str, optional, the label for the x-axis of the confusion matrix plot.
    - ylab: str, optional, the label for the y-axis of the confusion matrix plot.
    - output_path (str): Model Specific Path to save confusion matrix

    Returns:
    - None

    """
    # Compute the confusion matrix
    cm = confusion_matrix(y_ground, y_pred_ground, sample_weight=sample_weight, normalize=normalize)
    
    # Set the font size and figure size of the plot
    plt.rcParams.update({'font.size': 15})
    plt.rcParams.update({'figure.figsize':(10,10)})
    
    # Plot the heatmap of the confusion matrix using seaborn
    heatmap = sns.heatmap(cm, xticklabels=list(label_dict.keys()), yticklabels=list(label_dict.keys()), square=True, cmap="YlGnBu", annot=True, fmt='g', annot_kws={"fontsize": 12})
    
    # Set the x and y labels of the plot
    plt.xlabel(xlab)
    plt.ylabel(ylab)
    plt.title("Confusion Matrix (Actual vs Predicted)")
    
    # Show the plot
    plt.plot()
    plt.xticks(rotation = 30)
    plt.savefig(output_path+'confusion_matrix.png', bbox_inches = 'tight')
    plt.show()


label_dict_small = {'Cardio/Pul': 0, 'Gastro': 1, 'Neuro': 2}
def lime_prediction(model_name, text, save_path,label_dict_small, output_label = tuple(label_dict_small.values()), num_features=10, num_samples = 20):
    """
    Generate an explanation for a given text using LIME.

    Args:
        model_name (str): The name of the model to use for prediction.
        text (str): The input text to generate an explanation for.
        save_path (str): The path to save the LIME explanation to.
        label_dict_small (dict): A dictionary mapping label names to integer ids.
        output_label (tuple, optional): A tuple of label ids to generate explanations for. Defaults to all labels.
        num_features (int, optional): The number of features to include in the explanation. Defaults to 10.
        num_samples (int, optional): The number of samples to generate for each explanation. Defaults to 20.

    Returns:
        None
    """
    # Define the predictor function for LIME
    def predictor(texts):
        # Free up GPU memory and run garbage collection
        torch.cuda.empty_cache()
        gc.collect()

        # Set up the device and move the model to the GPU
        device = torch.device("cuda")
        torch.cuda.empty_cache()
        model.to(device)

        # Tokenize the input text and move it to the GPU
        input = tokenizer(texts, return_tensors="pt", padding=True)
        input.to(device)

        # Run the model and convert the logits to probabilities
        outputs = model(**input)
        tensor_logits = outputs[0]
        probas = F.softmax(tensor_logits.cpu()).detach().numpy()
        return probas

    # Load the model and tokenizer
    class_names = list(label_dict_small.keys())
    filename_model = './models/'+model_name
    tokenizer = AutoTokenizer.from_pretrained(filename_model)
    model = AutoModelForSequenceClassification.from_pretrained(filename_model)

    # Free up GPU memory and run garbage collection
    torch.cuda.empty_cache()
    gc.collect()

    # Set up the LIME explainer and generate the explanation
    explainer = LimeTextExplainer(class_names=class_names)
    exp = explainer.explain_instance(text, predictor, num_features=num_features, num_samples=num_samples, labels=output_label)

    # Display the explanation in the notebook and save it to a file
    exp.show_in_notebook(text=True, labels=output_label)
    exp.save_to_file(save_path)