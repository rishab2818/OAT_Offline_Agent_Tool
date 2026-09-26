procedure Select_AOA is

   L1L2_FAILED : BASE_DATA_TYPE.DISCRETE_FLAG;
   R1R2_FAILED : BASE_DATA_TYPE.DISCRETE_FLAG;

begin

   -- Left pair failure
   L1L2_FAILED :=
      AOA_MISTRACK_LTCH_LIST(L1) or
      AOA_MISTRACK_LTCH_LIST(L2) or
      LOCANG_EFAIL_LTCH_LIST(L1)  or
      LOCANG_EFAIL_LTCH_LIST(L2);

   -- Right pair failure
   R1R2_FAILED :=
      AOA_MISTRACK_LTCH_LIST(R1) or
      AOA_MISTRACK_LTCH_LIST(R2) or
      LOCANG_EFAIL_LTCH_LIST(R1)  or
      LOCANG_EFAIL_LTCH_LIST(R2);


   if (not L1L2_FAILED) and (not R1R2_FAILED) then

      -- Both sides healthy
      AOA_SELECTED :=
         (AOA_MFADP_VAL_LIST(L1) +
          AOA_MFADP_VAL_LIST(R1)) / 2.0;

   elsif (not L1L2_FAILED) and R1R2_FAILED then

      -- Right failed, use left
      AOA_SELECTED := AOA_MFADP_VAL_LIST(L1);

   elsif L1L2_FAILED and (not R1R2_FAILED) then

      -- Left failed, use right
      AOA_SELECTED := AOA_MFADP_VAL_LIST(R1);


   end if;

end Select_AOA;