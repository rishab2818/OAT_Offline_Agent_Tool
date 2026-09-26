procedure Level1_Fail is

   aoa_validity_list  : MFADP_DISC_LIST_TYPE;
   mistrack_supressor : BASE_DATA_TYPE.DISCRETE_FLAG;
   AOA_L1L2_MIST_TH_CONST : constant BASE_DATA_TYPE.SHORT_FLOAT := 5.0;
   AOA_R1R2_MIST_TH_CONST : constant BASE_DATA_TYPE.SHORT_FLOAT := 10.0;
begin
    aoa_validity_list := not(LOCANG_EFAIL_LTCH_LIST or
                             LOCANG_EFAIL_UNLTCH_LIST or
                             AOA_MISTRACK_LTCH_LIST);
    mistrack_supressor :=  aoa_ssa_comb_fail_supress and th_cas_mfadp;
    AOA_MISTRACK_UNLTCH1_LIST(L1) := abs(AOA_MFADP_VAL_LIST((L1)-AOA_MFADP_VAL_LIST(L2))) > AOA_L1L2_MIST_TH_CONST and mistrack_supressor;
    AOA_MISTRACK_UNLTCH1_LIST(R1) := abs(AOA_MFADP_VAL_LIST((R1)-AOA_MFADP_VAL_LIST(R2))) > AOA_R1R2_MIST_TH_CONST and mistrack_supressor;
end Level1_Fail;