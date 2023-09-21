import pandas as pd
import requests
import json
import numpy as np  # Import NumPy for NaN
import unittest
import sys



'''
How to run this test
Make sure first the Docker is running if not
(an example in my computer)
docker run -v /Users/jocotton/Desktop/references_files:/Users/jocotton/Desktop/references_files -p 8080:8080 -it spliceailookup_local
The app will be ready after displaying "Initialization completed."
Then, make sure you have the control dataframe in the current directory spliceai_control.csv
So far, this table has 158 control variants and their Spliceai annotation.
Type python3 TestingDocker.py

This performs two test for each variant. One test comaparing the four DS values from the API match the one in the table
The second test for the four DP values.

Manuel D.
'''

class DataProcessor:
    """
    This class is designed to hold the key data processing methods that will be applied to the DataFrame.
    By organizing these methods inside a class, we can easily extend, test, and maintain the code.
    """

    @staticmethod
    def compare_single_column(row, control_col, test_col):
        """
        This function compares two columns in a given row. 
        It returns True if they are equal, otherwise False.
        """
        return row[control_col] == row[test_col]

    @staticmethod
    def custom_compare(row, col1, col2):
        """
        Compares two columns in a row considering NaN values.
        This is required because in Python, NaN == NaN will return False.
        This function will return True if both values are NaN or if they are equal.
        """
        val1, val2 = row[col1], row[col2]
        if pd.isna(val1) and pd.isna(val2):
            return True
        return val1 == val2

    @staticmethod
    def fetch_scores_from_api(chr_value, pos_value, ref_value, alt_value):
        """
        Constructs the URL using the provided arguments, then fetches data from the API.
        It returns the parsed JSON response.
        """
        url = f"http://0.0.0.0:8080/spliceai/?hg=37&distance=50&mask=1&variant={chr_value}-{pos_value}-{ref_value}-{alt_value}&raw=chr{chr_value}-{pos_value}-{ref_value}-{alt_value}"
        response = requests.get(url)
        return json.loads(response.text)

    @staticmethod
    def update_dataframe_with_scores(df, index, score):
        """
        Given a DataFrame, an index, and a score from the API response,
        it updates the DataFrame with the required values and comparisons.
        """

        # Update the DataFrame with the values from the score dictionary
        df.at[index, 'DS_AG'] = round(float(score['DS_AG']), 2)
        df.at[index, 'DS_AL'] = round(float(score['DS_AL']), 2)
        df.at[index, 'DS_DG'] = round(float(score['DS_DG']), 2)
        df.at[index, 'DS_DL'] = round(float(score['DS_DL']), 2)
        df.at[index, 'DP_AG'] = score['DP_AG']
        df.at[index, 'DP_AL'] = score['DP_AL']
        df.at[index, 'DP_DG'] = score['DP_DG']
        df.at[index, 'DP_DL'] = score['DP_DL']

        # Check DS columns. If the DS value is 0.00, set the corresponding DP column to NaN.
        ds_cols = ['DS_AG', 'DS_AL', 'DS_DG', 'DS_DL']
        dp_cols = ['DP_AG', 'DP_AL', 'DP_DG', 'DP_DL']

        for ds_col, dp_col in zip(ds_cols, dp_cols):
            if df.at[index, ds_col] == 0.00:
                df.at[index, dp_col] = np.nan

        # Compare the DS columns between the control and test and add results to the DataFrame
        for control_col, test_col in zip(['DS_AG-CONTROL', 'DS_AL-CONTROL', 'DS_DG-CONTROL', 'DS_DL-CONTROL'],
                                        ['DS_AG', 'DS_AL', 'DS_DG', 'DS_DL']):
            new_col_name = f"Comparison_{test_col}"
            df.at[index, new_col_name] = DataProcessor.compare_single_column(df.loc[index], control_col, test_col)

        # Compare the DP columns, accounting for NaN values, between the control and test
        for control_col, test_col in zip(['DP_AG-CONTROL', 'DP_AL-CONTROL', 'DP_DG-CONTROL', 'DP_DL-CONTROL'],
                                        ['DP_AG', 'DP_AL', 'DP_DG', 'DP_DL']):
            new_col_name = f"Comparison_{test_col.split('_')[1]}"
            df.at[index, new_col_name] = DataProcessor.custom_compare(df.loc[index], control_col, test_col)

        return df


class TestDataProcessor(unittest.TestCase):
    """
    A set of unittests to ensure our DataProcessor methods are functioning as expected.
    """

    def test_compare_single_column(self):
        row = {'control': 5, 'test': 5}
        self.assertTrue(DataProcessor.compare_single_column(row, 'control', 'test'))

    def test_custom_compare(self):
        row = {'col1': np.nan, 'col2': np.nan}
        self.assertTrue(DataProcessor.custom_compare(row, 'col1', 'col2'))

    # ... more unit tests ...


if __name__ == "__main__":
    """
    Main execution flow. This script will:
    1. Read the CSV into a DataFrame.
    2. Loop through the DataFrame to fetch data from the API and update the DataFrame.
    3. Save the final DataFrame to a CSV.
    4. Run the unittests to ensure our processing methods are functioning correctly.
    """

    # 1. Reading the CSV into a DataFrame.
    df = pd.read_csv(sys.argv[1])

    print(f"Analysing and testing {len(df)} variants. This will take a while")
    columns_to_convert = ['DS_AG-CONTROL', 'DS_AL-CONTROL', 'DS_DG-CONTROL', 'DS_DL-CONTROL', 'DP_AG-CONTROL']
    df[columns_to_convert] = df[columns_to_convert].astype(float)

    for index, row in df.iterrows():
        print(f"Processing the variant {row['Chr']}:{row['Pos']}{row['Ref']}>{row['Alt']}")
    
        scores = DataProcessor.fetch_scores_from_api(row['Chr'], row['Pos'], row['Ref'], row['Alt'])
        for score in scores.get('scores', []):
            if 'WRGL4' in score.get('SYMBOL', ''):
                df = DataProcessor.update_dataframe_with_scores(df, index, score)
                break



    # 3. Save the final DataFrame to a CSV.
    df.to_csv('./results.csv', index=False)

    # 4. Running the unit tests.
    unittest.main(argv=['first-arg-is-ignored'], exit=False)  # This ensures the tests run in the script, and we ignore the first argument to unittest.main
