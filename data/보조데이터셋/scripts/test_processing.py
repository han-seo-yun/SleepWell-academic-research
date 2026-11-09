import unittest
import numpy as np
from build_reference_tables import zero_nonwear,weighted_quantile

class RulesTest(unittest.TestCase):
    def test_nonwear_and_sas_zero(self):
        self.assertEqual(zero_nonwear(np.r_[np.ones(5)*20,np.full(60,5e-79),np.ones(5)*20]).sum(),60)
        self.assertEqual(zero_nonwear(np.zeros(59)).sum(),0)
        self.assertEqual(zero_nonwear(np.r_[np.zeros(40),20,np.zeros(40)]).sum(),0)
    def test_quantile(self):
        self.assertEqual(weighted_quantile(np.array([3,1,2]),np.array([1,8,1]),.5),1)

if __name__=='__main__':unittest.main()
